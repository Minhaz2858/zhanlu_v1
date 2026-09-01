"""Agent conversations router — CRUD with real LLM-powered tool calling.

The SDK calls these via client.agents.getConversations(), createConversation(),
getConversation(), addMessage(), updateConversation():
  GET  /apps/{app_id}/agents/conversations           (list, optional ?agent_name=)
  POST /apps/{app_id}/agents/conversations            (create)
  GET  /apps/{app_id}/agents/conversations/{id}       (get by id)
  PUT  /apps/{app_id}/agents/conversations/{id}       (update metadata/status)
  POST /apps/{app_id}/agents/conversations/v2/{id}/messages  (add message + LLM reply)

Conversations are stored in the agent_conversations table with messages as JSON.
When a user message is added, the agent runtime:
  1. Loads the system prompt based on conversation.agent_name
  2. Calls the LLM with tool definitions (function calling)
  3. If the LLM requests a tool call, executes it (e.g. creates an AgentApp)
  4. Feeds the tool result back to the LLM for a final text response
  5. Returns the conversation with structured messages including tool_calls
"""

import asyncio
import json
import os
import re
import time
import unicodedata
import uuid
import httpx
import logging
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.config import settings
from app.deps import get_db, get_current_user_required
from app.models.agent_app import AgentApp
from app.models.agent_conversation import AgentConversation
from app.models.user import User
from app.services.tracing import TraceContext
from app.services.data_execution.session_state import SessionState, SessionStateService
from app.services.data_execution.cache import cache_data_execution
from app.services.data_execution.prompt_block import build_session_state_block
from app.services.tool_handlers.artifact_tool import _create_artifact_tool
from app.services.llm_service import llm_headers, llm_url, get_model, model_has_fixed_temperature
from app.services.llm_router import resolve_effective_llm, resolve_message_project_id, EffectiveLLM
from app.services.agent_prompts import (
    get_system_prompt,
    get_tools,
    apply_grounding_to_schemas,
    parse_decision_summary_block,
    strip_decision_summary_block,
    calculate_agent_budget,
)
# execute_tool_with_retry wraps execute_tool with result-level retry +
# one-shot argument reformulation (Phase B). All ReAct-loop call sites in
# this router go through it; handler-level exception retry still happens
# inside execute_tool itself. Signature-compatible alias.
from app.services.agent_tools import execute_tool_with_retry as execute_tool

# P0 reliability: per-turn loop guardrails, iteration budget, result persistence
from app.services.tool_loop_guardrails import (
    ToolLoopGuardController,
    ToolGuardrailConfig,
    synthetic_blocked_result as _guardrail_synthetic_result,
)
from app.services.iteration_budget import IterationBudget
from app.services.dashboard_turn_guard import (
    is_live_dashboard_request,
    should_force_create_dashboard,
    dashboard_guard_blocked_tools,
    dashboard_guard_should_block_queries,
    contract_confirmation_needed,
    dashboard_build_tool,
    fuzzy_dashboard_request,
    describe_schema_cap_reached,
    DASHBOARD_SCHEMA_CAP_TOOLS,
    DASHBOARD_ANTITOOLS,
    dashboard_antitools_should_block,
    DASHBOARD_EXPLORATION_TOOLS,
    dashboard_exploration_cap_reached,
    parse_artifact_title,
    dashboard_narration_needs_nudge,
    build_dashboard_narration_nudge_message,
    dashboard_orchestrator_should_block,
    verify_dashboard_build_produced_app,
)
from app.services.dashboard_intent import (
    dashboard_intent,
    set_dashboard_intent,
    reset_dashboard_intent,
)
from app.services.pptx_turn_guard import (
    pptx_turn_guard,
    should_force_create_pptx,
)
from app.services.file_turn_guard import (
    file_turn_guard,
    should_force_create_file,
    is_file_deliverable_request,
    file_artifact_created,
    build_file_disclosure,
)
from app.services.tool_result_persistence import (
    persist_tool_result,
    apply_turn_budget,
    budget_for_context_window,
)

# P1 reliability: structured error classification, pre-API pruning
from app.services.api_error_classifier import classify_api_error, FailoverReason
from app.services.llm_retry import (
    call_with_transient_retry,
    is_transient,
    max_attempts_for,
    next_backoff,
    retry_after_seconds,
)
from app.services.compaction.pre_api_prune import prune_tool_results_only

# P2 reliability: message sanitization, verification-on-stop, background review
from app.services.message_sanitization import sanitize_messages
from app.services.verification_stop import build_verify_on_stop_nudge
from app.services.background_review import spawn_background_review, DEFAULT_REVIEW_INTERVAL

# P3: prompt caching
from app.services.prompt_caching import apply_cache_control

# P2-12: shared agent-loop core extracted from this router (pure refactor).
# The tool-execution mechanics, SSE event builders, guardrails and canned
# fallback text live in ``app.services.agent_loop``; we re-import them here
# so every existing call site and ``from app.routers.agents import ...``
# consumer keeps working unchanged.
from app.services.agent_loop import (
    # tool_executor
    emit_tool_progress_while_waiting,
    execute_tool_batch,
    is_long_running_tool,
    # guardrails
    maybe_force_finish_line,
    # sse_builders
    LIVE_EVENT_TEMPLATES,
    PHASE_HEADLINES,
    _DATA_OFFER_MAX_COLS,
    _DATA_OFFER_MAX_ROWS,
    _LIVE_EVENT_CAP,
    _LIVE_EVENT_FORBIDDEN_RE,
    _LIVE_EVENT_SAFE_FALLBACK,
    _build_live_event,
    _emit_activity_step,
    _emit_live_event,
    _emit_phase,
    _sample_rows_from_payload,
    _sanitize_live_event_params,
    _sanitize_value,
    _sse_live_event,
    # fallbacks
    _APOLOGY_PATTERN_RE,
    _BOUNCE_BACK_PATTERN_RE,
    _DASHBOARD_REDIRECT_FALLBACK,
    _EMPTY_CONTENT_FALLBACK,
    _GENERIC_EMPTY_CONTENT_FALLBACK,
)

# P4: metrics + provider fallback
from app.services.agent_metrics import metrics
from app.services.provider_fallback import with_fallback

# P4: user-level LLM overrides (temperature, max_tokens, fallback model)
from app.services.user_settings_runtime import get_user_llm_overrides

# Import tool_handlers to trigger registration of all new tools in the registry
import app.services.tool_handlers  # noqa: F401

# Synexia report pipeline — synthesize_report enriches ask_data_agent results
# with a rich ReportCardPayload (KPIs, chart, insights, actions).
from app.services.synexia.report_synthesis import synthesize_report, merge_answer_rows  # noqa: E402
from app.services.synexia.finalize import finalize_into_artifact, build_no_data_payload  # noqa: E402

# Planning layer: should_trigger_planning classifies the user message and
# SynexiaFSM runs the cognitive loop (GOAL→PLAN→GATE→ACT→OBSERVE→VERIFY).
# Gated by is_fsm_enabled() (settings.SYNEXIA_FSM_ENABLED, default False).
from app.services.planning_trigger import should_trigger_planning, is_followup_refinement, PlanTrigger, _is_non_data_intent  # noqa: E402
from app.services.synexia.fsm import ExecutionRequest, SynexiaFSM, is_fsm_enabled  # noqa: E402
from app.services.synexia.context_assembler import build_conversation_context, format_followup_context_block  # noqa: E402
from app.services import steer_bus  # noqa: E402 — P2 mid-turn steer
from app.services.memory_consolidation import consolidate_turn_memory  # noqa: E402 — P3 post-turn memory consolidation

# Artifact marker contract: ◤MD_DOCX◤ / ◤HTML_DOCX◤ / ◤PPTX◤ ◤END_*◤ blocks
# in the assistant's reply are intercepted and routed through the generation
# orchestrator (app.services.generation_orchestrator), which awaits the async
# _create_artifact_tool pipeline so they produce the same preview/links as
# LLM-driven create_artifact tool calls. Stripped from visible text.
from app.services.synexia.intent_router import detect_file_intent  # noqa: E402
from app.services.goal_contract import (  # noqa: E402 — Goal-Contract closed loop
    RESULT_QUALITY_ASSUMED_OK,
    RESULT_QUALITY_NO_DATA,
    build_goal_contract,
    extract_tables_from_sql,
    is_effective_empty,
    is_metadata_only_rows,
    pending_action_phrase,
)
from app.services.turn_planner import (  # noqa: E402 — deterministic turn plan
    build_turn_plan,
    mark_final_step_completed,
    plan_completed_steps,
    plan_step_added_frame,
    plan_step_completed_frame,
    plan_to_system_block,
)
from app.services.query_purpose import (  # noqa: E402 — query-purpose classifier
    TableRoleResolver,
    classify_query_purpose,
)
from app.services.answer_verification import (  # noqa: E402 — universal self-eval gate
    build_gap_disclosure,
    build_replan_nudge,
    evaluate_answer,
)

logger = logging.getLogger(__name__)

# Compaction state per conversation (keyed by conversation_id)
_compaction_states: dict[str, "AutoCompactState"] = {}

router = APIRouter(tags=["agents"])

# ── Scalability (Part 2 Gap Analysis): Sync DB → async bridge ──────
# Wraps sync SQLAlchemy operations in asyncio.to_thread so they don't
# block the event loop. Used for message persistence, history loading,
# and conversation state updates — the three hot paths in async handlers.
import functools
import contextvars
def _run_db_sync(func, *args, **kwargs):
    """Run a sync DB function in a thread pool via asyncio.to_thread.

    Preserves the current task's context so that tracing identifiers
    (trace_id, user_id, etc.) survive the thread boundary.

    Usage::
        conv = await _run_db_sync(db.query, Conversation).filter(...)
        await _run_db_sync(db.commit)
    """
    # Snapshot current context
    ctx = contextvars.copy_context()
    def _wrapped():
        return func(*args, **kwargs)
    return asyncio.to_thread(_wrapped, context=ctx)

MAX_TOOL_ITERATIONS = 40

# Per-tool-name hard cap for tool-call loops. If the LLM calls the same
# tool N or more times within a single conversation (across all
# iterations), we inject a system nudge and break out of the loop to
# stop runaway behavior (e.g. the agent_builder looping forever on
# `skills` / `skills_hub` lookups). Set conservatively above 2 to
# tolerate legitimate "list → pick → retry" patterns but well below
# MAX_TOOL_ITERATIONS so a single bad tool cannot eat the whole budget.
TOOL_CALL_HARD_CAP = 10

# Canned final answer emitted when the LLM loop ends with NO content at
# all (historically tuned for the Agent Builder flow). Single source of
# truth: automation_executor compares run output against this constant to
# detect "the agent produced nothing real" — do not change the wording
# without updating it there too.
#
# P2-12: the constants + apology/bounce-back regexes below are defined in
# ``app.services.agent_loop.fallbacks`` and re-exported at the top of this
# module (see the ``from app.services.agent_loop import ...`` block).


def _collect_artifact_titles(
    tool_calls_for_frontend: list[dict],
    orch_created: list[dict],
) -> list[str]:
    """Best-effort list of artifact titles produced this turn.

    Pulls titles from orchestrator-created artifacts (``_orch_created``) and
    from artifact/dashboard tool results recorded in ``tool_calls_for_frontend``.
    Also scans for ``report_card_payload.title`` (data-agent results) and
    any other ``results.title`` / ``results.summary.title`` fields so that
    report-card artifacts are never hidden by the fallback.
    Deduplicated, order-preserving. Used by the artifact-aware fallback so an
    empty ``assistant_content`` never hides a successfully produced artifact.
    """
    titles: list[str] = []
    for art in (orch_created or []):
        t = (art or {}).get("title")
        if t:
            titles.append(str(t))
    for tc in (tool_calls_for_frontend or []):
        name = tc.get("name")
        result = tc.get("results") or {}
        if not isinstance(result, dict):
            continue
        # Dashboard artifact
        if name == "create_dashboard":
            art = result.get("artifact")
            if isinstance(art, dict) and art.get("title"):
                titles.append(str(art["title"]))
        # Generic artifact / sandbox
        elif name in ("create_artifact", "run_sandbox_skill"):
            if result.get("title"):
                titles.append(str(result["title"]))
        # Data-agent report card (ask_data_agent)
        rcp = result.get("report_card_payload")
        if isinstance(rcp, dict) and rcp.get("title"):
            titles.append(str(rcp["title"]))
        # Generic title / summary.title on any tool result
        if result.get("title") and not (name in ("create_artifact", "run_sandbox_skill")):
            titles.append(str(result["title"]))
        summary = result.get("summary")
        if isinstance(summary, dict) and summary.get("title"):
            titles.append(str(summary["title"]))
    seen: set[str] = set()
    out: list[str] = []
    for t in titles:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _phase_lock_should_block(parsed_call: dict) -> bool:
    """Decide whether the deliverable phase-lock should block one parsed tool call.

    Blocks ``create_artifact`` / ``run_sandbox_skill`` while the contract
    requires data and no answer-tagged dataset exists yet.

    Re-export exemption (2026-08-29): a ``create_artifact`` call carrying
    ``source_execution_id`` builds from the CACHED execution of a PREVIOUS
    turn — no new data collection is needed, so it must NOT be blocked.
    Otherwise the documented re-export path (prompt RE-EXPORT HARD RULE +
    SESSION STATE block) is impossible while the phase lock is on, and the
    agent flails (re-runs data, tries sandbox, or fabricates a completion).
    The tool itself TTL- and session-validates the id.
    """
    tool_name = parsed_call.get("tool_name")
    if tool_name not in ("create_artifact", "run_sandbox_skill"):
        return False
    if tool_name == "create_artifact":
        try:
            args = json.loads(parsed_call.get("args_str") or "{}")
        except (ValueError, TypeError):
            args = {}
        if (args or {}).get("source_execution_id"):
            return False
    return True


def _artifact_aware_fallback(titles: list[str]) -> str:
    """Build a user-facing message that references the produced artifact(s).

    Prefer this over ``_GENERIC_EMPTY_CONTENT_FALLBACK`` when the loop ended
    with empty assistant text but an artifact WAS produced — the artifact is
    the real deliverable, so the message should point at it instead of
    claiming the agent "had trouble putting it together".

    FIX 2026-08-23: previously returned a thin one-liner that started with
    "I've completed your request. Here's the artifact: …". That text
    *hid* the analysis the user actually wanted and was indistinguishable
    from a generic LLM fallback. Now we emit a more substantive opener
    that invites the user to scroll into the artifact / ask for
    clarification, while still naming the artifact.
    """
    if not titles:
        return (
            "Your deliverable is attached above. Open it for the full "
            "data, tables, and charts. If anything looks off, tell me "
            "what you'd like to refine (time range, grouping, metric) "
            "and I'll re-run."
        )
    if len(titles) == 1:
        return (
            f"Your deliverable — **{titles[0]}** — is attached above. "
            f"It contains the full data, tables, and charts I pulled "
            f"from your warehouse. Open the artifact for the complete "
            f"view; if any number, ranking, or chart needs a different "
            f"angle, tell me which one to refine."
        )
    joined = ", ".join(f"**{t}**" for t in titles)
    return (
        f"Your deliverables — {joined} — are attached above. "
        f"They contain the full data, tables, and charts I pulled from "
        f"your warehouse. Open the artifacts for the complete view; if "
        f"any number, ranking, or chart needs a different angle, tell "
        f"me which one to refine."
    )


# Tool names whose success indicates the agent scheduled a future
# deliverable via ``create_automation`` / ``update_automation``. Kept in
# sync with ``app.services.file_turn_guard._AUTOMATION_SCHEDULING_TOOLS``.
_AUTOMATION_SCHEDULING_TOOL_NAMES = frozenset(
    {
        "create_automation",
        "update_automation",
        "AutomationTask.create",
        "AutomationTask.update",
    }
)


def _automation_scheduled_confirmation(
    tool_calls_for_frontend,
    fmt: str,
) -> str:
    """Short, honest confirmation shown when the agent scheduled an
    automation task but emitted empty assistant content (so the loop
    falls back to a placeholder text).

    Deterministic — pulls the task name / schedule / project from the
    ``create_automation`` args blob. No synthetic LLM call (which would
    just burn budget). Used by ``_choose_fallback`` so the user gets a
    real confirmation, NOT the misleading "I gathered some information
    but had trouble putting it all together…" line.

    Pinned by tests in
    ``tests/test_automation_scheduled_confirmation.py``. See the
    2026-08-25 Daily Sales Data Sync regression: this helper was
    drafted earlier in the day then lost in the P2-12
    fallback-extraction refactor (it never landed in the
    ``agent_loop.fallbacks`` extraction); we now keep it here in the
    router-local pattern alongside ``_artifact_aware_fallback`` /
    ``_data_summary_fallback``.
    """
    import json as _json

    name = ""
    schedule = ""
    project = ""
    for tc in tool_calls_for_frontend or []:
        if str(tc.get("name") or "") not in _AUTOMATION_SCHEDULING_TOOL_NAMES:
            continue
        raw = tc.get("arguments_string") or tc.get("arguments") or ""
        if isinstance(raw, dict):
            raw = _json.dumps(raw)
        if not raw or not isinstance(raw, str):
            continue
        try:
            obj = (
                _json.loads(raw)
                if raw.lstrip().startswith(("{", "["))
                else {}
            )
        except Exception:
            obj = {}
        if not isinstance(obj, dict):
            obj = {}
        name = name or str(obj.get("name") or "")
        schedule = schedule or str(obj.get("schedule") or "")
        project = project or str(obj.get("project") or "")
    label = (fmt or "").upper() or "FILE"
    title = name or "the automation task"
    sched_hint = (
        f" Schedule: `{schedule}` (cron expression; the scheduler "
        f"computes the next fire)." if schedule else ""
    )
    proj_hint = f" Project: **{project}**." if project else ""
    return (
        f"**{title}** has been scheduled.{proj_hint} The {label} "
        f"deliverable will be produced automatically by the runtime "
        f"agent at the next cron fire — no further action is needed "
        f"from you in this chat session.{sched_hint} You can review "
        f"or pause the task from the Automation panel."
    )


def _data_summary_fallback(titles: list[str], user_content: str = "") -> str:
    """Fallback when data-agent report cards exist but synthesis text is empty.

    This is more specific than the generic fallback: it names the reports
    that were produced and avoids the misleading "create dashboard" redirect
    unless the user actually asked for a dashboard.
    """
    if len(titles) == 1:
        return (
            f"I produced **{titles[0]}** above with the full data. "
            f"Let me know if you'd like a more detailed analysis or a combined dashboard."
        )
    joined = ", ".join(f"**{t}**" for t in titles)
    return (
        f"I produced {joined} above with the full supply chain data. "
        f"Let me know if you'd like a single combined dashboard or a deeper analysis."
    )


def _cad_build_fallback(tool_calls_for_frontend: list[dict]) -> str | None:
    """Deterministic CAD build summary (2026-08-27).

    The CAD Agent's Fusion tool loop returns structured, machine-readable
    results (fusion360_verify_build PASS/FAIL + body bboxes, fusion360_info
    scene dumps). The local LLM (qwen3.6-27b) frequently fails to synthesize
    these into prose and emits the generic apology instead — so when a turn
    actually ran Fusion 360 tools, build the answer deterministically from
    the tool results rather than letting the model (or the generic fallback)
    speak.

    Returns a user-facing summary, or None when the turn had no Fusion 360
    activity (caller falls through to the normal fallback chain).
    """
    if not tool_calls_for_frontend:
        return None
    fusion_calls = [
        tc for tc in tool_calls_for_frontend
        if isinstance(tc.get("name"), str) and tc["name"].startswith("fusion360")
    ]
    if not fusion_calls:
        return None

    def _res(tc: dict) -> dict:
        r = tc.get("results") or {}
        return r if isinstance(r, dict) else {}

    any_success = any(_res(tc).get("success") for tc in fusion_calls)

    if not any_success:
        # Every Fusion call failed — surface the bridge-help text.
        err = ""
        for tc in reversed(fusion_calls):
            e = _res(tc).get("error") or ""
            if e:
                err = e
                break
        if "not reachable" in err or "add-in" in err or "bridge" in err.lower():
            return (
                "Fusion 360 is not reachable right now. Open Autodesk Fusion 360 "
                "and start the FusionMCP add-in (Tools > Add-Ins > Scripts and "
                "Add-Ins > Add-Ins tab > FusionMCP > Run), then ask me to build again."
            )
        return "The Fusion 360 build did not complete: %s" % (err[:300] or "unknown error")

    # Build succeeded — report from verify_build (ground truth) when present.
    verify = next(
        (tc for tc in reversed(fusion_calls) if tc.get("name") == "fusion360_verify_build"),
        None,
    )
    if verify and _res(verify).get("ok"):
        v = _res(verify)
        parts = []
        for b in (v.get("bodies") or []):
            bb = b.get("bbox_mm")
            if isinstance(bb, (list, tuple)) and len(bb) == 6:
                w = abs(bb[3] - bb[0])
                d = abs(bb[4] - bb[1])
                h = abs(bb[5] - bb[2])
                label = "box" if b.get("faces") == 6 else ("cylindrical" if b.get("faces") == 3 else "solid")
                parts.append("%d×%d×%d mm %s" % (round(w), round(d), round(h), label))
            else:
                parts.append("body %d" % (b.get("index", "?")))
        body_txt = ", ".join(parts) if parts else "%d bodies" % v.get("body_count", 0)
        return (
            "Build complete and verified (PASS): %s on the Fusion 360 canvas."
            % body_txt
        )
    if verify:
        # verify ran but returned FAIL — surface the REAL reasons (count /
        # duplicates / params / dimension mismatches) instead of guessing.
        v = _res(verify)
        issues = v.get("issues") or []
        dc = v.get("dimension_checks") or []
        txt = "; ".join(str(i) for i in issues) if issues else "unknown"
        if dc:
            bad = [c for c in dc if not c.get("ok")]
            if bad:
                txt += "; measured: " + "; ".join(
                    "%s %s" % (c.get("body_index"), c.get("actual")) for c in bad
                )
        return (
            "The build did NOT pass verification: %s. Fix the discrepancy "
            "and re-verify, or tell me exactly what is wrong." % txt
        )

    # No verify_build but info ran — report the body count from the scene dump.
    # 2026-08-28: ONLY trust the info count when no modeling tool ran AFTER the
    # last info call. A build that sketched→extruded after the info shows a
    # stale count (observed: info said 0 bodies, then extrude built the part —
    # the fallback wrongly told the user the canvas was empty).
    info = next(
        (tc for tc in reversed(fusion_calls) if tc.get("name") == "fusion360_info"),
        None,
    )
    if info:
        _info_idx = fusion_calls.index(info)
        _modeled_after_info = any(
            tc.get("name") not in _FUSION_READONLY_TOOLS
            and i > _info_idx
            for i, tc in enumerate(fusion_calls)
        )
    else:
        _modeled_after_info = False
    stdout = _res(info).get("stdout") or "" if info else ""
    if not _modeled_after_info and "BODIES:" in stdout:
        try:
            nb = int(stdout.split("BODIES:")[1].split()[0])
            return (
                "Build complete — %d bod%s on the Fusion 360 canvas."
                % (nb, "y" if nb == 1 else "ies")
            )
        except Exception:  # noqa: BLE001
            pass
    if _modeled_after_info:
        return (
            "Fusion 360 modeling tools ran this turn, but the build was not "
            "verified (no fusion360_verify_build call). The model may be on the "
            "canvas — call fusion360_info or fusion360_verify_build to inspect "
            "the current scene and confirm it matches what you asked for."
        )
    return "Fusion 360 tools ran this turn, but the build was not verified. Use fusion360_info to inspect the current model and confirm it matches what you asked for."


# Fusion 360 tools that never create/modify geometry. Used by the CAD
# destructive-reclear guard: a successful modeling tool in THIS turn means
# a model is being built, so a later `fusion360_clear` would wipe it.
_FUSION_READONLY_TOOLS = frozenset({
    "fusion360_clear", "fusion360_ping", "fusion360_info", "fusion360_project",
    "fusion360_verify_build", "fusion360_lookup_api",
    "fusion360_make_drawing", "fusion360_export_geometry", "fusion360_save",
    "fusion360_user_parameter", "fusion360_measure", "fusion360_physical_properties",
})


def _fusion_build_progress_this_turn(tool_calls_for_frontend: list[dict]) -> bool:
    """True when a non-readonly fusion360 tool succeeded this turn (build in progress).

    Read-only tools (info/ping/project/verify/lookup/export/drawing/param) do
    NOT count — they never create or modify geometry, so a clear after them is
    legitimate. Any other fusion360 tool (sketch_*, extrude, revolve, box,
    cylinder, sweep, loft, coil, fillet, thread, hole, ...) with success=True
    means the model is being built — a subsequent clear would wipe it.
    """
    for tc in tool_calls_for_frontend or []:
        name = tc.get("name")
        if not (isinstance(name, str) and name.startswith("fusion360")):
            continue
        if name in _FUSION_READONLY_TOOLS:
            continue
        r = tc.get("results") or {}
        if isinstance(r, dict) and r.get("success"):
            return True
    return False



def _choose_fallback(
    tool_calls_for_frontend: list[dict],
    orch_created: list[dict],
    user_content: str = "",
) -> str:
    """Choose the best fallback message based on what was produced this turn.

    Priority:
    0. If Fusion 360 tools ran this turn → ``_cad_build_fallback`` (deterministic)
    1. If data-agent report cards exist → ``_data_summary_fallback``
    2. If any artifact exists → ``_artifact_aware_fallback``
    3. If user asked for a dashboard → ``_DASHBOARD_REDIRECT_FALLBACK``
    4. Otherwise → ``_GENERIC_EMPTY_CONTENT_FALLBACK``
    """
    # FIX 2026-08-27 (CAD): a Fusion tool loop returns structured results the
    # local LLM often fails to summarize (it emits the generic apology). Build
    # the answer deterministically from the tool results instead.
    _cad_msg = _cad_build_fallback(tool_calls_for_frontend)
    if _cad_msg:
        return _cad_msg
    titles = _collect_artifact_titles(tool_calls_for_frontend, orch_created)
    # Check if any data-agent report cards or raw rows were produced
    from app.services.goal_contract import is_metadata_only_rows  # lazy
    has_report_card = False
    has_rows = False
    for tc in (tool_calls_for_frontend or []):
        result = tc.get("results") or {}
        if not isinstance(result, dict):
            continue
        if result.get("report_card_payload"):
            has_report_card = True
        if result.get("rows"):
            rows = result["rows"]
            # Metadata-only rows (MIN_DATE/MAX_DATE/ENTRY_COUNT) don't
            # count as real business data.
            if isinstance(rows, list) and len(rows) > 0 and is_metadata_only_rows(rows):
                continue
            has_rows = True
        if has_report_card and has_rows:
            break
    if has_report_card and titles:
        return _data_summary_fallback(titles, user_content)
    if titles:
        return _artifact_aware_fallback(titles)
    if _is_dashboard_request(user_content):
        return _DASHBOARD_REDIRECT_FALLBACK
    # FIX 2026-08-24: check has_rows BEFORE the file-deliverable redirect.
    # Previously the file-disclosure ("The requested HTML report could not
    # be generated…") fired first even when real data rows HAD been
    # retrieved — the user saw "could not be generated" while the data sat
    # unused. Now: if rows exist, show them; the file-disclosure only
    # fires when there is truly nothing to show.
    if has_rows:
        return _data_rows_fallback(tool_calls_for_frontend, user_content=user_content)
    # FIX 2026-08-24 (probe-only): when data queries DID run but every
    # result was metadata-only (SELECT COUNT(*) / MIN / MAX probes), the
    # generic file-disclosure ("could not be generated… tool budget") is
    # misleading — the budget wasn't the problem, the queries never
    # fetched business rows. Say that explicitly so the user knows to
    # rephrase instead of just retrying.
    _probe_only = False
    for tc in (tool_calls_for_frontend or []):
        if tc.get("name") not in DATA_PRODUCING_TOOLS:
            continue
        _pr = tc.get("results") or {}
        if not isinstance(_pr, dict):
            continue
        _prows = _pr.get("rows")
        if (
            isinstance(_prows, list)
            and _prows
            and len(_prows) <= 2
            and is_metadata_only_rows(_prows)
        ):
            _probe_only = True
            break
    if _probe_only:
        return (
            "I queried your warehouse, but the data agent only ran summary "
            "probes (row counts / date ranges) instead of fetching actual "
            "business rows, so there is nothing to build the report from. "
            "Please try again — I will query specific business columns "
            "(revenue, volume, margin, inventory) directly this time."
        )
    # File-deliverable redirect: when the user asked for a file deliverable
    # (html/docx/pdf/xlsx/md) but no artifact was created AND no data rows
    # were retrieved, return a format-specific message instead of the
    # generic fallback.
    #
    # IMPORTANT (2026-08-25): also suppress the disclosure when the user
    # asked the AGENT (not the runtime) to "set up a daily task with
    # output_format=html" and the agent already scheduled it via
    # ``create_automation``. ``file_artifact_created`` is now
    # automation-aware (returns True for create_automation/update_automation
    # calls that scheduled the matching format) so this branch correctly
    # skips the misleading "could not be generated within this turn's
    # tool budget" line — and ``_automation_scheduled_confirmation``
    # surfaces the actual task name + schedule + project so the user
    # sees a real confirmation, not the generic "I gathered some
    # information but had trouble putting it all together" apology.
    _is_file, _fmt = is_file_deliverable_request(user_content)
    if _is_file and _fmt:
        if file_artifact_created(tool_calls_for_frontend, _fmt):
            return _automation_scheduled_confirmation(
                tool_calls_for_frontend, _fmt
            )
        return build_file_disclosure(_fmt)
    return _GENERIC_EMPTY_CONTENT_FALLBACK


_SYS_COL_PREFIXES = ("FENTRYID", "FID", "FCUSTMATID", "FCUSTMATNAME")


def _is_degenerate_dataset(rows: list[dict] | None) -> bool:
    """True when every row has zero / None / "" in all apparent measure columns.

    A 'measure' column is any key that looks monetary (revenue, margin, amount,
    price, total, sales) but NOT a count/quantity column (line_count, qty,
    quantity, count, orders) which may legitimately be non-zero while the money
    columns are broken.  Used to prevent building a report card around a query
    that mapped the wrong column (e.g. revenue = 0 for every row).
    """
    rows = rows or []
    if not rows:
        return True
    _measure_re = re.compile(
        r"^(revenue|margin|amount|price|total|sales|income|profit|cost)$",
        re.IGNORECASE,
    )
    _count_re = re.compile(r"^(line_count|qty|quantity|count|orders|num)$", re.IGNORECASE)
    measure_cols: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            for k in row:
                if _measure_re.search(k) and not _count_re.search(k):
                    measure_cols.add(k)
    if not measure_cols:
        return False  # no measure columns to judge by
    for row in rows:
        if not isinstance(row, dict):
            continue
        for col in measure_cols:
            v = row.get(col)
            if v not in (None, "", 0, 0.0):
                return False
    return True


def _resolve_regenerate_turn(
    messages: list[dict],
    user_role: str,
    user_content: str,
    file_urls: list,
    selected_skill,
    selected_skill_id,
):
    """Resolve a ``regenerate: true`` turn by REUSING the last user message.

    When the user clicks Regenerate, the frontend re-sends the SAME user
    content — the original user message (with its file_urls / skill) is
    already in ``conv.messages``. This helper walks back to it, copies its
    context (content, uploaded files, skill) into the current turn so the
    regenerated run is contextually identical to the original, then pops
    the previous assistant reply so the model does not see its own old
    answer (ChatGPT/Kimi replace the prior answer, they don't continue
    from it) and the history stays [.., user, NEW assistant].

    Pure function over the messages list (mutates it by popping the last
    assistant message) — unit-testable without a DB.
    """
    for _m in reversed(messages):
        if _m.get("role") == user_role:
            if not user_content:
                user_content = _m.get("content") or ""
            if not file_urls:
                _prev_urls = _m.get("file_urls") or []
                file_urls = [
                    u for u in _prev_urls
                    if isinstance(u, str) and u.startswith("/api/uploads/")
                ]
            if not selected_skill and not selected_skill_id:
                _prev_skill = _m.get("selected_skill")
                _prev_skill_id = _m.get("selected_skill_id")
                if _prev_skill:
                    selected_skill = _prev_skill
                if _prev_skill_id:
                    selected_skill_id = _prev_skill_id
            break
    # Pop the previous assistant reply so (a) the model does not see
    # its own old answer while regenerating, and (b) the conversation
    # history stays [.., user, NEW assistant] with no stale duplicates.
    for _i in range(len(messages) - 1, -1, -1):
        if messages[_i].get("role") == "assistant":
            messages.pop(_i)
            break
    return user_content, file_urls, selected_skill, selected_skill_id


def _extract_citations_from_tool_calls(tool_calls: list | None) -> list[dict]:
    """Extract Kimi/GPT-style data-source citations from a turn's tool calls.

    Walks the tool calls a run produced (FSM ``tool_calls`` with a
    ``result`` key carrying ``source_id`` / ``source_name`` / ``rows``,
    or legacy ``tool_calls_for_frontend`` with a ``results`` dict) and
    collects unique, human-readable sources the answer is grounded in.
    Each citation is ``{"source_id": str|None, "source_name": str,
    "rows": int|None}`` plus optional ``url`` (clickable link) and
    ``kind`` (``web`` / ``file``) fields:
    - web_search results become web chips (title + live URL), capped at 5.
    - file-bearing tool results (create_artifact / docx / read_file on an
      upload) become file chips (file_name + file_url when safe).
    Defensive: malformed shapes are skipped, and the list is deduped by
    source id/name so repeated queries against the same datasource
    render once.
    """
    citations: list[dict] = []
    # source key → index into `citations` (keeps the first name seen, lets
    # later calls upgrade the row count).
    seen: dict = {}

    def _add_source(src_id, src_name, rows):
        # source_name may arrive as a nested OBJECT (e.g.
        # ``result["source"]`` is a dict {id, name, db_type}) — never
        # feed a dict into the seen-set (unhashable) or into the chip.
        if not isinstance(src_id, str):
            src_id = None
        if isinstance(src_name, dict):
            src_name = src_name.get("name") or src_name.get("id") or src_name.get("label") or None
        if not isinstance(src_name, str):
            src_name = None
        key = src_id or src_name or ""
        if not key:
            return
        if key in seen:
            # A turn can call the same datasource multiple times (probe
            # query, then the real query). Keep the most informative row
            # count — a later non-zero count beats an earlier probe's 0,
            # otherwise the chip shows "0 rows" for a source that DID
            # return data.
            idx = seen[key]
            existing = citations[idx].get("rows")
            if rows is not None and (existing is None or rows > existing):
                citations[idx]["rows"] = rows
            return
        seen[key] = len(citations)
        citations.append({
            "source_id": src_id,
            "source_name": src_name or src_id or "data source",
            "rows": rows,
        })

    for tc in tool_calls or []:
        if not isinstance(tc, dict):
            continue
        # FSM shape: {"result": <result_data>}; legacy: {"results": <result>}
        result = tc.get("result") if isinstance(tc.get("result"), dict) else tc.get("results")
        if not isinstance(result, dict):
            continue
        # ── Web-search citations (Kimi/GPT-style live source links) ──────
        # web_search returns {"success", "query", "results": [{title, url,
        # description}], "count"} — none of the data-source fields below.
        # Detect by shape (a top-level "query" string + url-bearing result
        # rows) so fetch_data_batch / datasource shapes never match here.
        _web_q = result.get("query")
        _web_rows = result.get("results")
        if (
            isinstance(_web_q, str)
            and isinstance(_web_rows, list)
            and any(isinstance(s, dict) and isinstance(s.get("url"), str) for s in _web_rows)
        ):
            _web_added = 0
            for _sub in _web_rows:
                if not isinstance(_sub, dict):
                    continue
                _url = _sub.get("url")
                if not isinstance(_url, str) or not _url.startswith(("http://", "https://")):
                    continue
                _title = _sub.get("title") or _sub.get("name") or _url
                if isinstance(_title, str):
                    _title = _title.strip()
                if not _title:
                    _title = _url
                _key = f"web:{_url}"
                if _key in seen:
                    continue
                seen[_key] = len(citations)
                citations.append({
                    "source_id": _url,
                    "source_name": (_title[:120] + "…") if len(_title) > 120 else _title,
                    "rows": None,
                    "url": _url,
                    "kind": "web",
                })
                _web_added += 1
                if _web_added >= 5:
                    break
            continue
        src_id = result.get("source_id")
        src_name = (
            result.get("source_name")
            or result.get("source")
            or result.get("kb_name")
            or result.get("database_name")
        )
        rows = result.get("rows")
        if isinstance(rows, (list, tuple)):
            row_count = len(rows)
        elif isinstance(rows, int):
            row_count = rows
        else:
            row_count = None
        if src_id or src_name:
            _add_source(src_id, src_name, row_count)
        # fetch_data_batch / multi-query shapes: nested results[*]
        sub_results = result.get("results")
        if isinstance(sub_results, list):
            for sub in sub_results:
                if not isinstance(sub, dict):
                    continue
                sub_rows = sub.get("rows")
                if isinstance(sub_rows, (list, tuple)):
                    sub_row_count = len(sub_rows)
                elif isinstance(sub_rows, int):
                    sub_row_count = sub_rows
                else:
                    sub_row_count = None
                sub_name = (
                    sub.get("source_name")
                    or sub.get("source")
                    or sub.get("label")
                    or sub.get("kb_name")
                    or sub.get("database_name")
                )
                sub_id = sub.get("source_id")
                if sub_id or sub_name:
                    _add_source(sub_id, sub_name, sub_row_count)
        # ── File-bearing tool results (uploaded / generated files) ───────
        # Tools that produced a file (create_artifact, docx/xlsx/pptx
        # export, read_file on an upload) carry file_url/file_name —
        # surface as a clickable file chip when the URL is safe.
        _file_url = result.get("file_url")
        _file_name = result.get("file_name")
        if (
            isinstance(_file_url, str)
            and _file_url.startswith(("/api/uploads/", "http://", "https://"))
        ):
            _fkey = f"file:{_file_url}"
            if _fkey not in seen:
                seen[_fkey] = len(citations)
                citations.append({
                    "source_id": _file_url,
                    "source_name": (_file_name.strip() if isinstance(_file_name, str) and _file_name.strip() else "file"),
                    "rows": None,
                    "url": _file_url,
                    "kind": "file",
                })
        elif isinstance(_file_name, str) and _file_name.strip():
            _fkey2 = f"file:{_file_name.strip()}"
            if _fkey2 not in seen:
                seen[_fkey2] = len(citations)
                citations.append({
                    "source_id": None,
                    "source_name": _file_name.strip(),
                    "rows": None,
                    "kind": "file",
                })
    return citations


def _extract_data_rows_from_tool_call(tc: dict) -> tuple[list[dict], str] | None:
    """Best-effort extraction of (rows, source_label) from a tool call.

    Handles BOTH shapes:
    - Tools like ``ask_data_agent`` / ``execute_query`` whose top-level
      ``result["rows"]`` is the row list and ``result["source_name"]``
      is the human-readable data source label.
    - ``fetch_data_batch`` whose ``result["results"]`` is a list of
      sub-query dicts, each carrying its own ``"rows"`` and ``"label"``.

    Returns (rows, source_label) on success, ``None`` when this tool
    call has no usable rows. Defensive: bad / malformed shapes are
    skipped silently rather than blowing up the fallback path.
    """
    result = tc.get("results") or {}
    if not isinstance(result, dict):
        return None
    # Shape A — direct rows at top level (ask_data_agent, execute_query, …).
    direct_rows = result.get("rows")
    if isinstance(direct_rows, (list, tuple)) and direct_rows:
        src = (
            result.get("source_name")
            or result.get("source")
            or result.get("kb_name")
            or result.get("database_name")
            or "the data source"
        )
        return (list(direct_rows), src)
    # Shape B — fetch_data_batch: nested ``results[*].rows``.
    sub_results = result.get("results")
    if isinstance(sub_results, list):
        for sub in sub_results:
            if not isinstance(sub, dict):
                continue
            sub_rows = sub.get("rows")
            if isinstance(sub_rows, (list, tuple)) and sub_rows:
                # Use the first non-empty sub-query as the primary one.
                return (list(sub_rows), str(sub.get("label") or "the data source"))
    return None


# 2026-08-26: strip-residue threshold. The post-loop hygiene strips
# (promise / internal-reference / SQL-narration) can leave a SHORT fragment
# behind (e.g. "Let me check the warehouse…", a partial sentence) that is
# neither empty nor a recognized placeholder prefix. Such residue must not
# block the deferred synthesis prose from reaching the bubble — otherwise
# the user sees a mangled fragment while the real analysis sits unused.
# A genuinely short answer (e.g. "¥165.03M, up 5.7%.") stays untouched:
# the replacement rule below requires the synthesis candidate to be
# substantially richer than the residue.
_WEAK_CONTENT_MAX_CHARS = 160
_WEAK_CONTENT_SYNTH_MIN_CHARS = 400


def _is_weak_strip_residue(acc_text: str, synth_text: str) -> bool:
    """True when ``acc_text`` looks like stripped residue AND ``synth_text``
    is a substantially richer candidate that should replace it."""
    return (
        0 < len(acc_text) < _WEAK_CONTENT_MAX_CHARS
        and len(synth_text) >= _WEAK_CONTENT_SYNTH_MIN_CHARS
    )


def _data_rows_fallback(
    tool_calls_for_frontend: list[dict],
    user_content: str | None = None,
) -> str:
    """Direct fallback built from the actual retrieved rows (no LLM).

    Renders a clean markdown table (≤10 rows), hiding system columns
    (FENTRYID, FCUSTMATID, …) and all-empty columns. Never surfaces
    raw "Sample values: FENTRYID=…" junk. Now handles BOTH the direct
    ``result["rows"]`` shape (ask_data_agent / execute_query) and the
    nested ``fetch_data_batch`` ``result["results"][*].rows`` shape —
    see ``_extract_data_rows_from_tool_call``.

    2026-08-26: when the user asked for a written report/review/
    performance/etc. and the synthesis LLM call failed, instead of
    returning only "Analyzing N rows…" (a useless placeholder), we
    now build a brief best-effort analysis from the row aggregates
    so the user gets a meaningful response even when the LLM
    synthesis times out. The platform data card is still attached
    separately, so the user sees BOTH the data + a narrative.
    """
    from app.services.goal_contract import is_metadata_only_rows  # lazy
    for tc in (tool_calls_for_frontend or []):
        if tc.get("name") not in DATA_PRODUCING_TOOLS:
            continue
        extracted = _extract_data_rows_from_tool_call(tc)
        if extracted is None:
            continue
        rows, src = extracted
        if isinstance(rows, (list, tuple)):
            n = len(rows)
        if isinstance(rows, (list, tuple)):
            n = len(rows)
            # Flatten to dict rows
            dict_rows = [r for r in rows if isinstance(r, dict)]
            if not dict_rows:
                continue
            # Metadata-only rows (MIN_DATE/MAX_DATE/ENTRY_COUNT) are not
            # meaningful business data — show a targeted message instead.
            # 2026-08-26: when the user asked for a written REPORT, expand
            # this into a full narrative explaining what was returned,
            # what the date range means, and exactly how to ask for the
            # detail. The user explicitly wants extensive answers.
            if is_metadata_only_rows(dict_rows):
                # Extract the values from the metadata row for context
                meta_row = dict_rows[0]
                min_d = max_d = None
                rc = None
                for k, v in meta_row.items():
                    kn = (k or "").lower()
                    if "min" in kn and ("date" in kn or "time" in kn):
                        min_d = str(v)[:10]
                    elif "max" in kn and ("date" in kn or "time" in kn):
                        max_d = str(v)[:10]
                    elif "count" in kn or "entry" in kn or "row" in kn:
                        if isinstance(v, (int, float)):
                            rc = int(v)
                # Build a long-form explanation
                out_lines: list[str] = []
                out_lines.append("**Data Scope Note — only summary metadata was returned**\n")
                out_lines.append(
                    f"The data tool answered your question with aggregate "
                    f"information about the **{src}** dataset rather than "
                    f"individual business records. This usually means the "
                    f"SQL behind the question ended with an aggregation "
                    f"(e.g. `SELECT MIN(date), MAX(date), COUNT(*) FROM …`) "
                    f"without a `GROUP BY`, so only one summary row came back."
                )
                out_lines.append("")
                # What we DO know
                out_lines.append("**What the data tells us**\n")
                if rc is not None:
                    out_lines.append(
                        f"- **{_format_num(rc)} records** exist in the underlying table for the queried scope."
                    )
                if min_d and max_d:
                    span = _date_span_days(min_d, max_d)
                    if span is not None:
                        out_lines.append(
                            f"- Date span: **{min_d} to {max_d}** "
                            f"({span} days, {span // 30 if span >= 30 else span} "
                            f"{'months' if span >= 30 else 'days'})."
                        )
                    else:
                        out_lines.append(f"- Date span: **{min_d} to {max_d}**.")
                if not (rc is not None or (min_d and max_d)):
                    out_lines.append(
                        "- The returned row only contains metadata columns; no business fields are present."
                    )
                out_lines.append("")
                # How to fix
                out_lines.append("**How to get the actual report**\n")
                out_lines.append(
                    f"- {L_rephrase_1} by adding dimensions "
                    f"such as *customer*, *product*, *region*, or *contract type* "
                    f"to the `GROUP BY` clause so the query returns multiple rows."
                )
                out_lines.append(
                    f"- {L_rephrase_2} — for example, ask for "
                    f"`contract value by customer` or `top 20 products by revenue`."
                )
                if min_d and max_d:
                    out_lines.append(
                        f"- {L_rephrase_3} — your underlying data covers "
                        f"`{min_d}` to `{max_d}`, so the question needs a "
                        f"`WHERE` filter that matches your intended reporting period."
                    )
                out_lines.append(
                    f"- {L_rephrase_4} — confirm the question is asking "
                    f"for transaction-level data, not a `COUNT(*)` over the whole table."
                )
                return "\n".join(out_lines)
            # Hide system columns
            hide_cols = {
                k for k in dict_rows[0]
                if k.startswith(_SYS_COL_PREFIXES) or k in _SYS_COL_PREFIXES
            }
            # Hide all-empty columns
            for k in dict_rows[0]:
                if all(
                    r.get(k) in (None, "", 0, 0.0)
                    for r in dict_rows
                ):
                    hide_cols.add(k)
            visible = [k for k in dict_rows[0] if k not in hide_cols]
            if not visible:
                visible = [k for k in dict_rows[0]]  # fallback: show at least something
            lines: list[str] = []
            lines.append("| " + " | ".join(visible) + " |")
            lines.append("|" + "|".join(" --- " for _ in visible) + "|")
            for r in dict_rows[:10]:
                lines.append(
                    "| " + " | ".join(str(r.get(k, "")) for k in visible) + " |"
                )
            # 2026-08-26: when the user asked for a written report,
            # build a brief best-effort narrative from the actual row
            # aggregates instead of returning just the placeholder
            # "Analyzing N rows…". The data card is attached separately
            # by the platform, so the user sees BOTH the data table and
            # the narrative. This guarantees the user always gets a
            # useful written answer for report-style requests even when
            # 2026-08-26: the LLM synthesis call times out or returns
            # empty, the platform's empty-bubble guarantee fires and
            # displays the fallback as the user's answer. We used to
            # only run the aggregate fallback for explicit report-style
            # requests (report/review/performance/etc.), but the user
            # wants extensive analysis for ALL data queries — including
            # "snapshot", "summary", "stats", plain data asks, etc.
            # So we now ALWAYS run the aggregate fallback when there are
            # actual data rows. The user gets a real written narrative
            # around the data card, never just "Analyzing N rows…".
            # 2026-08-26: business-aware deterministic report FIRST — for
            # known intents (contract performance) build the real business
            # report from the rows (contracted vs delivered, execution
            # rate, top customers, MoM) instead of generic stats. The
            # report uses ## headers so the frontend suppresses the raw
            # data table and shows the report as the answer.
            try:
                from app.services.db.business_reports import (
                    try_build_business_report,
                )  # lazy

                biz_report = try_build_business_report(
                    user_content or "", dict_rows, src,
                )
                if biz_report:
                    return biz_report
            except Exception as _biz_exc:
                logger.warning(
                    "_data_rows_fallback: business report builder failed: %s",
                    _biz_exc,
                )
            try:
                return _build_aggregate_fallback(dict_rows, src, user_content or "")
            except Exception as _fallback_exc:
                logger.warning(
                    "_data_rows_fallback: _build_aggregate_fallback "
                    "failed (rows=%d, src=%s): %s",
                    len(dict_rows), src, _fallback_exc,
                )
            # FIX 2026-08-22: return a neutral placeholder instead of a
            # markdown table so the user doesn't see raw data mid-stream.
            # The synthesis step replaces this with the final narrative
            # (which may include a summary table).  The frontend
            # DataTableCard (gated by !isStreaming) still renders the
            # data after streaming if synthesis didn't run.
            return f"Analyzing {len(rows)} rows of data…"
    return _GENERIC_EMPTY_CONTENT_FALLBACK


# 2026-08-26: business-scenario detection — used to label numbers in
# terms the user understands (contract value, fulfillment rate, etc.)
# instead of column names. Matches both English and Chinese keywords.
# 2026-08-26 (round 7): removed the hardcoded business-scenario
# detection (_BUSINESS_SCENARIO_PATTERNS / _detect_business_scenario).
# The previous approach was keyword-based (contract / sales /
# shipment / inventory / production / pricing) and failed for any
# domain outside that list (HR, medical, education, finance,
# logistics, etc.).
#
# New approach: GENERIC, column-name-driven. The fallback derives
# every label from the actual column names, so it works for ANY
# database without code changes:
#   - total_contract_amount → "Total Contract Amount"
#   - total_revenue         → "Total Revenue"
#   - patient_count         → "Patient Count"
#   - enrollment_total      → "Enrollment Total"
# The LLM in the primary path is still the one that does the
# real intent understanding — the fallback is just a safety net
# when the LLM fails.


def _humanize_column_name(name: str) -> str:
    """Convert a snake_case column name to a human-readable label.

    Examples:
      total_contract_amount → Total Contract Amount
      customer_id           → Customer
      product_name          → Product Name
      unit_price            → Unit Price
      平均金额                → 平均金额 (Chinese: pass through)
    """
    if not name:
        return ""
    s = str(name).strip().replace("-", "_").replace("__", "_")
    # Chinese characters — pass through, just strip common suffixes
    if any("\u4e00" <= c <= "\u9fff" for c in s):
        return s
    # Strip common suffixes that don't add meaning
    for suffix in ("_id", "_uuid", "_code", "_no", "_number", "_count"):
        if s.lower().endswith(suffix):
            s = s[: -len(suffix)]
            break
    # snake_case → Title Case
    parts = [p for p in s.split("_") if p]
    return " ".join(p.capitalize() for p in parts)


def _infer_headline_metric(columns_by_role: dict[str, list[str]], user_msg: str) -> str | None:
    """Pick the best headline metric from the classified columns.

    Priority: money > quantity > kpi > first generic numeric.
    Tie-break by absolute sum (largest first). Returns the column
    name, not the humanized label.
    """
    # Priority order
    for role in ("money", "quantity", "kpi"):
        cols = columns_by_role.get(role, [])
        if cols:
            # Return the one with the largest sum (computed by caller
            # via the pick_by_sum helper)
            return cols[0]
    return columns_by_role.get("generic", [None])[0] if columns_by_role.get("generic") else None


def _pluralize(noun: str, count: int) -> str:
    """English-friendly plural for a noun. Handles Chinese (no s) by
    passing through, and adds 's' for English nouns."""
    if not noun:
        return "records"
    if any("\u4e00" <= c <= "\u9fff" for c in noun):
        return noun  # Chinese: no pluralization
    # Already plural (ends in s)
    if noun.endswith("s"):
        return noun
    return noun + ("s" if count != 1 else "")


# 2026-08-26: business-scenario column classification — picks money,
# quantity, KPI, and ID columns so we never surface IDs (customer_id,
# contract_id) as "Key Numbers".
_MONEY_KEYWORDS = (
    "amount", "revenue", "value", "price", "cost", "fee", "total",
    "payment", "sum", "money", "sales_amount", "sales_revenue",
    "contract_amount", "contract_value", "contract_revenue",
    "shipped_amount", "margin_amount",
    "金额", "总额", "总值", "总价", "货款", "销售额", "金额", "总价",
)
_QUANTITY_KEYWORDS = (
    "quantity", "qty", "count", "volume", "weight", "tons", "tonnes",
    "kg", "liters", "units",
    "数量", "重量", "吨", "公斤", "升",
)
_KPI_KEYWORDS = (
    "margin", "profit", "rate", "percent", "ratio", "pct", "fulfillment",
    "on_time", "yield", "growth",
    "毛利率", "利润率", "完成率", "增长率", "履约率",
)
_ID_KEYWORDS = (
    "_id", "id_", "id", "code", "number", "no.", "no_", "fid", "fentryid",
    "fcustmat", "hash", "uuid", "guid", "key",
)
_DATE_KEYWORDS = ("date", "time", "day", "month", "year", "日期", "时间", "年月")


def _classify_column(name: str, sample_value) -> str:
    """Classify a column into a business role.

    Returns one of: "id" | "date" | "money" | "quantity" | "kpi" |
                    "generic" (numeric, role unclear).
    """
    n = (name or "").lower()
    # ID first (most specific)
    for kw in _ID_KEYWORDS:
        if kw in n or n.endswith(kw) or n == kw:
            return "id"
    # Date
    for kw in _DATE_KEYWORDS:
        if kw in n:
            return "date"
    # Money
    for kw in _MONEY_KEYWORDS:
        if kw in n:
            return "money"
    # Quantity
    for kw in _QUANTITY_KEYWORDS:
        if kw in n:
            return "quantity"
    # KPI / margin / rate
    for kw in _KPI_KEYWORDS:
        if kw in n:
            return "kpi"
    # Fall back on value shape
    if isinstance(sample_value, str) and sample_value:
        # ISO date
        if len(sample_value) >= 10 and sample_value[4:5] == "-" and sample_value[7:8] == "-":
            return "date"
    return "generic"


def _format_money(v: float) -> str:
    # Report-consistent money: M for >= 1M, plain with commas below.
    # Avoid mixing 万 (Chinese unit) with M/plain inside one report.
    if abs(v) >= 1_000_000_000:
        return f"¥{v/1_000_000_000:,.2f}B"
    if abs(v) >= 1_000_000:
        return f"¥{v/1_000_000:,.2f}M"
    return f"¥{v:,.2f}"


def _format_qty(v: float, unit_hint: str = "") -> str:
    # Plain comma-separated quantity with optional unit; never 万 — the
    # mixed unit style reads inconsistently next to M-formatted money.
    return f"{v:,.2f} {unit_hint}".strip()


# 2026-08-26: helpers for the metadata-only fallback — used to build
# the "what the data tells us" + "how to fix" sections of the
# narrative returned when the SQL ended with a bare aggregate.
def _format_num(v: int | float) -> str:
    if v is None:
        return "0"
    if abs(v) >= 10_000:
        return f"{v/10_000:,.2f}万"
    return f"{int(v):,}"


L_rephrase_1 = "Rephrase the request"
L_rephrase_2 = "Be more specific about the grouping"
L_rephrase_3 = "Adjust the date filter"
L_rephrase_4 = "Avoid bare COUNT(*) over the whole table"
L_underlying_1 = "records exist in the underlying table for the queried scope"


def _date_span_days(date_min: str, date_max: str) -> int | None:
    """Number of days between two YYYY-MM-DD strings. None on parse error."""
    from datetime import date as _date
    try:
        d1 = _date.fromisoformat(date_min[:10])
        d2 = _date.fromisoformat(date_max[:10])
        return (d2 - d1).days
    except Exception:
        return None


def _stddev(vals: list[float]) -> float:
    """Population standard deviation."""
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    return (sum((v - mean) ** 2 for v in vals) / len(vals)) ** 0.5


def _build_aggregate_fallback(
    dict_rows: list[dict],
    src: str,
    user_msg: str,
) -> str:
    """2026-08-26: business-scenario-aware narrative built from the
    actual retrieved rows. Used when the LLM synthesis call fails on
    a report-style request.

    Round-3 fix: classify columns into business roles (id/date/money/
    quantity/kpi) so we never surface ID columns (customer_id,
    contract_id) as "Key Numbers". Translate numbers into business
    terms appropriate to the detected scenario (contract value,
    fulfillment rate, top customers, etc.). The narrative is
    presented confidently without exposing the implementation detail
    that the synthesis service failed.
    """
    if not dict_rows:
        return _GENERIC_EMPTY_CONTENT_FALLBACK

    # ── 0. Honesty guard — a single metadata/summary row with NO numeric
    #    values (e.g. an aggregate over zero rows, a MAX(date) probe, or a
    #    COUNT(*) = 0) is NOT a real report. Narration would fabricate a
    #    confident story ("0 records spanning X to X") from thin air. Say
    #    so plainly instead, with the query source, so the user knows the
    #    bound data source simply has no data for what they asked.
    _row0 = dict_rows[0]
    _has_numeric = any(
        isinstance(v, (int, float)) for r in dict_rows for v in r.values()
    )
    if len(dict_rows) == 1 and not _has_numeric:
        _zh = any("\u4e00" <= ch <= "\u9fff" for ch in (user_msg or ""))
        _scope = " ".join(
            str(v) for v in _row0.values() if v is not None and str(v).strip()
        )
        _snippet = f" ({_scope})" if _scope else ""
        return (
            (
                f"No data found for the requested period/scope in `{src}`. "
                f"The only row returned was a metadata/summary row{_snippet} "
                "with no actual numbers — the bound data source has no "
                "matching records (table may be stale or the period has no "
                "data). Try a different date range, or ask to check which "
                "table/data source holds the data."
            )
            if not _zh
            else (
                f"在 `{src}` 中未找到所请求时段/范围的数据。"
                f"返回的唯一一行是元数据/汇总行{_snippet}，不含任何实际数值——"
                "说明绑定的数据源没有匹配记录（表数据可能已过期，或该时段无数据）。"
                "请尝试其他日期范围，或询问数据存放在哪张表/哪个数据源。"
            )
        )

    # ── 1. Classify every column ───────────────────────────────────
    first = dict_rows[0]
    col_role: dict[str, str] = {}
    for k in first:
        col_role[k] = _classify_column(k, first.get(k))

    money_cols = [k for k, r in col_role.items() if r == "money"]
    qty_cols = [k for k, r in col_role.items() if r == "quantity"]
    kpi_cols = [k for k, r in col_role.items() if r == "kpi"]
    id_cols = [k for k, r in col_role.items() if r == "id"]
    date_cols = [k for k, r in col_role.items() if r == "date"]
    # Generic numeric (role unclear) — still include if it's big
    generic_num_cols = [
        k for k, r in col_role.items()
        if r == "generic" and isinstance(first.get(k), (int, float))
    ]

    # ── 2. Pick display columns (top 3 by absolute sum per role) ───
    def _top_by_sum(cols: list[str], n: int = 3) -> list[str]:
        sums = []
        for k in cols:
            vals = [r.get(k) for r in dict_rows if isinstance(r.get(k), (int, float))]
            if vals:
                sums.append((k, sum(vals)))
        sums.sort(key=lambda kv: -abs(kv[1]))
        return [k for k, _ in sums[:n]]

    money_pick = _top_by_sum(money_cols, 3)
    qty_pick = _top_by_sum(qty_cols, 2)
    kpi_pick = _top_by_sum(kpi_cols, 2)
    generic_pick = _top_by_sum(generic_num_cols, 1)
    # Date column = the one with the most variance
    date_pick = date_cols[0] if date_cols else None
    if not date_pick:
        for k in first:
            v = first.get(k)
            if isinstance(v, str) and len(v) >= 10 and v[4:5] == "-" and v[7:8] == "-":
                date_pick = k
                break

    # ── 3. Pick the best label column for "top performers" (the
    #    non-ID label with the highest cardinality) ────────────────
    label_pick = None
    best_card = 0
    for k in first:
        if col_role.get(k) in ("id", "date", "money", "quantity", "kpi"):
            continue
        uniq = {r.get(k) for r in dict_rows if r.get(k)}
        if 1 < len(uniq) <= len(dict_rows) and len(uniq) > best_card:
            label_pick = k
            best_card = len(uniq)

    # ── 4. Pick the best label column for "top performers" + the
    #    best generic noun for the dataset (DERIVED from the data,
    #    not from a hardcoded business scenario) ─────────────────
    # 2026-08-26 (round 7): replaced _detect_business_scenario with
    # a generic, data-driven approach. The "noun" used in the report
    # (e.g. "contracts", "orders", "patients") is now derived from
    # the label column's humanized name, so it works for ANY
    # database without code changes:
    #   - customer column → "customers"
    #   - patient column  → "patients"
    #   - product column  → "products"
    #   - order column    → "orders"
    # The "headline metric" comes from the column with the largest
    # sum in the priority order money > quantity > kpi.
    label_pick = None
    best_card = 0
    for k in first:
        if col_role.get(k) in ("id", "date", "money", "quantity", "kpi"):
            continue
        uniq = {r.get(k) for r in dict_rows if r.get(k)}
        if 1 < len(uniq) <= len(dict_rows) and len(uniq) > best_card:
            label_pick = k
            best_card = len(uniq)
    # If no label column, try ID columns
    if not label_pick:
        for k in first:
            if col_role.get(k) == "id":
                uniq = {r.get(k) for r in dict_rows if r.get(k)}
                if 1 < len(uniq) <= len(dict_rows):
                    label_pick = k
                    break

    # Build the labels GENERICALLY from the actual data
    headline_metric = _infer_headline_metric(
        {
            "money": money_pick,
            "quantity": qty_pick,
            "kpi": kpi_pick,
            "generic": generic_pick,
        },
        user_msg,
    )
    # Use the headline metric's humanized name as the "value label"
    headline_label = _humanize_column_name(headline_metric) if headline_metric else None
    # Use the label column's humanized name as the "noun"
    noun_singular = _humanize_column_name(label_pick) if label_pick else "Record"
    if not noun_singular or noun_singular.lower() in ("", "id"):
        noun_singular = "Record"
    noun_plural = _pluralize(noun_singular, len(dict_rows))

    # Detect language
    is_chinese = any("\u4e00" <= ch <= "\u9fff" for ch in (user_msg or ""))
    L = lambda en, zh: zh if is_chinese else en

    # ── 5. Build the report (EXTENSIVE — 400-800 words minimum) ──
    out: list[str] = []

    # ── 5a. Compute common aggregates for the narrative ───────────
    # headline_metric already computed above from column roles (round 7 - generic)
    headline_vals: list = []
    if headline_metric:
        headline_vals = [r.get(headline_metric) for r in dict_rows if isinstance(r.get(headline_metric), (int, float))]
    headline_total = sum(headline_vals) if headline_vals else 0
    headline_avg = (headline_total / len(headline_vals)) if headline_vals else 0

    # Per-day counts and date span
    day_counter: dict[str, float] = {}
    for r in dict_rows:
        if not date_pick:
            break
        d = str(r.get(date_pick) or "")[:10]
        if not d:
            continue
        if headline_metric and headline_metric in money_cols:
            v = r.get(headline_metric)
            if isinstance(v, (int, float)):
                day_counter[d] = day_counter.get(d, 0.0) + v
        elif headline_metric:
            v = r.get(headline_metric)
            if isinstance(v, (int, float)):
                day_counter[d] = day_counter.get(d, 0.0) + v
        else:
            day_counter[d] = day_counter.get(d, 0.0) + 1

    # Date range from data
    date_min = date_max = None
    if date_pick:
        dates = sorted([str(r.get(date_pick) or "")[:10] for r in dict_rows if r.get(date_pick)])
        dates = [d for d in dates if d]
        if dates:
            date_min = dates[0]
            date_max = dates[-1]

    # Header — title derived from the user's actual request when possible
    # ("give me top customer in docx" → "Top Customer Report"), falling
    # back to a generic label. Avoids a generic "Data Report" on every
    # deliverable regardless of what was asked.
    _title = ""
    if user_msg:
        _t = re.sub(r"\s+", " ", (user_msg or "").strip())
        _t = re.sub(
            r"\s+(in|as|using|into)\s+(a\s+)?(docx|word|pdf|pptx|powerpoint|excel|xlsx|"
            r"markdown|md|html|file|document|deck|spreadsheet|formate?|format)\s*"
            r"(file|document|deck|spreadsheet)?\s*[\\.\\?]?\s*$",
            "", _t, flags=re.IGNORECASE,
        ).strip()
        _t = re.sub(
            r"^(please\s+|can\s+you\s+|i\s+want\s+|i\s+need\s+|give\s+me\s+|"
            r"show\s+me\s+|make\s+me\s+|generate\s+|create\s+)\s*",
            "", _t, flags=re.IGNORECASE,
        ).strip()
        if _t and len(_t) >= 3:
            _title = _t[0].upper() + _t[1:]
            _title = re.sub(r"\s+", " ", _title).rstrip(" .?!")
            if not _title.lower().endswith("report"):
                _title = f"{_title} Report"
    out.append(f"# {L(_title or 'Data Report', _title or '数据报告')}\n")
    out.append(
        f"**{L('Scope', '范围')}:** {len(dict_rows)} {noun_plural} "
        f"{L('from', '来自')} `{src}`"
        + (f" {L('covering', '覆盖')} `{date_min}` {L('to', '至')} `{date_max}`" if date_min and date_max else "")
        + ".\n"
    )

    # ── 5b. Executive Summary (4-6 sentence paragraph) ────────────
    summary_sentences: list[str] = []
    if headline_metric and headline_vals:
        # Generic, data-driven label — no hardcoded scenario terms.
        # Avoid "Total Total Revenue": the column may already be named
        # "Total Revenue" / "总金额", so strip a leading "Total " / "总计"
        # before prepending our own label.
        _base_label = (headline_label or headline_metric or "").strip()
        if _base_label.lower().startswith("total "):
            _base_label = _base_label[6:].strip()
        elif _base_label.startswith("总计"):
            _base_label = _base_label[2:].strip()
        value_label_en = f"Total {_base_label}" if _base_label else "Total value"
        value_label_zh = (
            _base_label
            if _base_label and any("\u4e00" <= c <= "\u9fff" for c in _base_label)
            else f"{_base_label or '数值'}总额"
        )
        if headline_metric in money_cols:
            summary_sentences.append(
                f"{L(value_label_en, value_label_zh)} "
                f"{L('over the period was', '为')} **{_format_money(headline_total)}** "
                f"{L('across', '共')} {len(dict_rows)} {noun_plural}, "
                f"{L('with an average of', '平均每笔')} **{_format_money(headline_avg)}** "
                f"{L('per record', '')}."
            )
            summary_sentences.append(
                f"{L('The single largest', '单笔最大金额为')} "
                f"**{_format_money(max(headline_vals))}** "
                f"{L('and the smallest', '，最小为')} "
                f"**{_format_money(min(headline_vals))}**, "
                f"{L('giving a wide range that suggests significant variation in record sizes', '，说明各记录规模差异较大')}."
            )
        else:
            summary_sentences.append(
                f"{L('Total', '总计')} {headline_label or headline_metric} "
                f"{L('over the period was', '为')} **{headline_total:,.2f}** "
                f"{L('across', '共')} {len(dict_rows)} {noun_plural}, "
                f"{L('with an average of', '平均每笔')} **{headline_avg:,.2f}**."
            )
    elif len(dict_rows) == 1 and date_min and date_max:
        summary_sentences.append(
            f"{L('The query returned a summary row covering', '本次查询返回了一行汇总记录，涵盖')} "
            f"**{len(headline_vals)} {L('records', '条记录')}** {L('spanning', '时间跨度')} "
            f"`{date_min}` {L('to', '至')} `{date_max}` "
            f"({_date_span_days(date_min, date_max)} {L('days', '天')})."
        )
    else:
        summary_sentences.append(
            f"The query returned {len(dict_rows)} {noun_plural} from `{src}`."
        )

    # Add distribution insight
    if headline_vals and len(headline_vals) >= 3:
        sorted_vals = sorted(headline_vals, reverse=True)
        top3_share = sum(sorted_vals[:3]) / headline_total * 100 if headline_total else 0
        if top3_share > 50:
            summary_sentences.append(
                f"{L('Concentration is high', '集中度较高')} — "
                f"{L('the top 3 records account for', '前 3 条记录占')} "
                f"**{top3_share:.1f}%** {L('of the total', '的总额')}, "
                f"{L('suggesting revenue/value is driven by a small number of large records', '说明总额主要由少数大额记录驱动')}."
            )
        else:
            summary_sentences.append(
                f"{L('Distribution is fairly even', '分布较为均衡')} — "
                f"{L('the top 3 records account for', '前 3 条记录占')} "
                f"**{top3_share:.1f}%** {L('of the total', '的总额')}, "
                f"{L('with no single record dominating', '没有单一记录占据主导')}."
            )

    # Add period insight
    if date_min and date_max and date_min != date_max:
        span = _date_span_days(date_min, date_max)
        if span and headline_total:
            if headline_metric in money_cols:
                per_day = _format_money(headline_total / max(span, 1))
            else:
                per_day = f"{headline_total / max(span, 1):,.2f}"
            summary_sentences.append(
                f"{L('Over the', '在')} {span} {L('day span', '天跨度内')} "
                f"({date_min} {L('to', '至')} {date_max}), {L('the average daily value was', '日均值为')} "
                f"**{per_day}**."
            )
    out.append("**" + L("Executive Summary", "执行摘要") + "**\n")
    out.append(" ".join(summary_sentences))
    out.append("")

    # ── 5c. Key Numbers (money, quantity, kpi) ────────────────────
    if money_pick:
        out.append(f"**{L('Revenue & Amounts', '金额指标')}**\n")
        for k in money_pick:
            vals = [r.get(k) for r in dict_rows if isinstance(r.get(k), (int, float))]
            if not vals:
                continue
            total = sum(vals)
            avg = total / len(vals)
            mx = max(vals)
            mn = min(vals)
            stddev = _stddev(vals)
            spread_pct = ((mx - mn) / avg * 100) if avg else 0
            out.append(
                f"- **{k}** — total {_format_money(total)}, average {_format_money(avg)}, "
                f"range {_format_money(mn)} – {_format_money(mx)} "
                f"({L('spread', '波动')}: {spread_pct:,.0f}%, std dev {_format_money(stddev)})."
            )
        out.append("")

    if qty_pick:
        out.append(f"**{L('Volumes & Quantities', '数量指标')}**\n")
        for k in qty_pick:
            vals = [r.get(k) for r in dict_rows if isinstance(r.get(k), (int, float))]
            if not vals:
                continue
            total = sum(vals)
            avg = total / len(vals)
            mx = max(vals)
            mn = min(vals)
            out.append(
                f"- **{k}** — total {_format_qty(total)}, average {_format_qty(avg)}, "
                f"max {_format_qty(mx)}, min {_format_qty(mn)}."
            )
        out.append("")

    if kpi_pick:
        out.append(f"**{L('Margins & Rates', '毛利与比率')}**\n")
        for k in kpi_pick:
            vals = [r.get(k) for r in dict_rows if isinstance(r.get(k), (int, float))]
            if not vals:
                continue
            avg = sum(vals) / len(vals)
            mx = max(vals)
            mn = min(vals)
            out.append(
                f"- **{k}** — average {avg:,.2f}, range {mn:,.2f} – {mx:,.2f}."
            )
        out.append("")

    # ── 5d. Trends & Comparisons (time pattern) ───────────────────
    out.append(f"**{L('Trends & Comparisons', '趋势与对比')}**\n")
    if day_counter:
        sorted_days = sorted(day_counter.items(), key=lambda kv: -kv[1])
        out.append(
            f"- {L('Active days', '活跃天数')}: **{len(day_counter)}**; "
            f"{L('Peak day', '高峰日')}: `{sorted_days[0][0]}` "
            f"({(_format_money(sorted_days[0][1]) if headline_metric in money_cols else f'{sorted_days[0][1]:,.0f}')})."
        )
        if len(sorted_days) >= 2:
            out.append(
                f"- {L('Slowest day', '最低日')}: `{sorted_days[-1][0]}` "
                f"({(_format_money(sorted_days[-1][1]) if headline_metric in money_cols else f'{sorted_days[-1][1]:,.0f}')})."
            )
        # Concentration: top 3 days vs rest
        if len(sorted_days) >= 3:
            top3 = sum(v for _, v in sorted_days[:3])
            rest = sum(v for _, v in sorted_days[3:])
            if rest > 0:
                ratio = top3 / rest
                out.append(
                    f"- {L('Day concentration', '日度集中度')}: "
                    f"{L('top 3 days', '前 3 天')} = {top3:,.0f} vs {L('rest', '其余')} = {rest:,.0f} "
                    f"({L('ratio', '比例')} {ratio:.2f}:1)."
                )
    elif date_min and date_max:
        out.append(
            f"- {L('Date range', '日期范围')}: `{date_min}` {L('to', '至')} `{date_max}` "
            f"({_date_span_days(date_min, date_max) or 0} {L('days', '天')})."
        )
    else:
        out.append(
            f"- {L('No time dimension detected in the data', '数据中未检测到时间维度')}."
        )
    # Single-day note
    if day_counter and len(day_counter) == 1:
        out.append(
            f"- {L('All activity concentrated on a single day', '所有活动集中在单一天')} "
            f"({list(day_counter.keys())[0]})."
        )
    out.append("")

    # ── 5e. Notable Anomalies (outlier detection) ─────────────────
    if headline_vals and len(headline_vals) >= 4:
        avg = sum(headline_vals) / len(headline_vals)
        stddev = _stddev(headline_vals)
        if stddev > 0:
            outliers = [v for v in headline_vals if abs(v - avg) > 2 * stddev]
            if outliers:
                if headline_metric in money_cols:
                    out.append(
                        f"- {len(outliers)} {L('outlier(s) detected', '个离群值')} "
                        f"({L('values beyond 2 standard deviations from the mean of', '超出均值 2 个标准差')} "
                        f"{_format_money(avg)}): {', '.join(_format_money(v) for v in outliers[:3])}"
                        f"{('…' if len(outliers) > 3 else '')}."
                    )
                else:
                    out.append(
                        f"- {len(outliers)} outlier(s) detected: "
                        f"{', '.join(f'{v:,.2f}' for v in outliers[:3])}"
                        f"{('…' if len(outliers) > 3 else '')}."
                    )
    # Empty-value rate
    if headline_metric:
        present = sum(1 for r in dict_rows if r.get(headline_metric) not in (None, "", 0, 0.0))
        missing = len(dict_rows) - present
        if missing > 0 and missing < len(dict_rows):
            pct = missing / len(dict_rows) * 100
            out.append(
                f"- {L('Data quality', '数据完整度')}: "
                f"**{missing} {L('rows', '行')} ({pct:.0f}%)** "
                f"{L('have missing or zero values for', '在')} `{headline_metric}`."
            )

    if not money_pick and not qty_pick and not kpi_pick and not day_counter:
        out.append(
            f"- {L('No numeric measures or time columns were detected in the retrieved data', '未在数据中检测到数值或时间列')}."
        )
    out.append("")

    # ── 5f. Top Performers (or sample rows if metadata) ───────────
    if headline_metric and label_pick and len(dict_rows) > 1:
        sort_key = headline_metric
        try:
            sorted_rows = sorted(
                [r for r in dict_rows if isinstance(r.get(sort_key), (int, float))],
                key=lambda r: r.get(sort_key, 0),
                reverse=True,
            )
        except Exception:
            sorted_rows = []
        if sorted_rows:
            out.append(f"**{L('Top Performers', '业绩排行')} ({L('by', '按')} {sort_key})**\n")
            for r in sorted_rows[:3]:
                v = r.get(sort_key)
                label = r.get(label_pick) or "(no label)"
                if sort_key in money_cols:
                    val_str = _format_money(v)
                else:
                    val_str = f"{v:,.2f}"
                out.append(f"- {label} — {sort_key} = {val_str}")
            out.append("")

    # Recommendations — scenario-aware (always 5+ actionable items)
    out.append(f"**{L('Recommended Next Steps', '后续建议')}**\n")
    recs = []
    # 2026-08-26 (round 7): removed scenario == "contract" check —
    # generic: apply to ANY dataset that has a money metric
    if money_pick:
        top_money = money_pick[0]
        _drill_en = "to identify which records drove the bulk of the value"
        _drill_zh = "，找出拉动总额的主要记录"
        recs.append(
            f"- {L('Drill into the top items', '深入分析头部记录')} by `{top_money}` "
            f"{L(_drill_en, _drill_zh)}."
        )
    if label_pick and (money_pick or headline_metric):
        # 2026-08-26 (round 7): scenario check removed — apply this
        # rec generically whenever we have a label column + a metric
        _rank_label = _humanize_column_name(label_pick) or "items"
        recs.append(
            f"- {L('Compare top items', '对比头部记录')} "
            f"({L('e.g.', '例如')} **{L('top 3 by', '前三按')} {headline_metric}**) "
            f"{L('against the prior period to spot retention, churn, or growth', '与上一周期对比，留意留存、流失或增长信号')}."
        )
    if date_pick:
        recs.append(
            f"- {L('Investigate the slowest day', '排查表现最弱的一天')} (`{date_pick}`) "
            f"{L('for operational issues — staffing, supply, or customer behavior', '，关注运营、供应或客户行为层面的原因')}."
        )
    if kpi_pick:
        kpi = kpi_pick[0]
        recs.append(
            f"- {L('Review', '复盘')} **{kpi}** "
            f"{L('by segment to find which product/customer mix is below target', '按业务维度拆分，找出低于目标的产品/客户组合')}."
        )
    # Always-on recommendations (independent of scenario)
    _validate_en = "adjust the WHERE clause if last month's data is incomplete or mixed with prior periods"
    _validate_zh = "如上月数据不完整或混入其他期间，请调整 WHERE 条件"
    recs.append(
        f"- {L('Validate the data scope', '核对数据口径')}: "
        f"{L('confirm that', '确认')} `{date_min or 'the date range'}` {L('matches the period you intended', '与目标统计区间一致')}; "
        f"{L(_validate_en, _validate_zh)}."
    )
    recs.append(
        f"- {L('Request a YoY / MoM comparison', '请求同比或环比对比')}: "
        f"{L('ask for the same query for the prior period to calculate growth rate', '用同一查询拉取上一周期数据，计算增长率')}."
    )
    recs.append(
        f"- {L('Drill into a specific segment', '深入到细分维度')}: "
        f"{L('filter by customer, product, region, or sales rep to find where the value is concentrated', '按客户/产品/区域/销售员等维度下钻，找到价值集中点')}."
    )
    if len(dict_rows) <= 1:
        recs.append(
            f"- {L('Expand the result set', '扩大返回数据')}: "
            f"{L('only', '仅')} {len(dict_rows)} {L('summary row was returned', '行汇总被返回')}; "
            f"{L('ask for the underlying detail (remove aggregations or add grouping) to see individual records', '请求明细数据（去掉聚合或增加分组），查看单条记录')}."
        )
    out.extend(recs)
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Institutional-Grade Research-Analyst Fallback (2026-08-25).
# Triggered when the research-analyst directive is active AND the
# main agent loop exited with empty ``assistant_content`` despite tool
# calls producing data. Common pattern with weak / non-directive-
# compliant models (e.g. deepseek-chat): the LLM emits
# ``tool_calls`` with empty content instead of synthesizing, the loop
# burns its budget on tool calls, exits, and the user sees only the
# raw data rows (the markdown-table fallback).
#
# This helper closes that gap: when invoked, it makes ONE focused
# synthesis LLM call with a tight institutional-grade prompt + the
# collected data summary, and returns the analysis text. If anything
# fails (timeout, error, no data, missing flag, non-DB agent), it
# returns ``None`` so the existing fallback chain runs unchanged.
# ---------------------------------------------------------------------------

async def _research_analyst_fallback(
    user_content: str,
    tool_calls_for_frontend: list[dict],
    agent_name: str | None,
    agent_app,
    max_data_chars: int = 6000,
    timeout_s: float = 60.0,
    endpoint=None,
) -> str | None:
    """One-shot forced-synthesis for the institutional-grade protocol.

    Returns:
        The institutional-grade analysis text on success, otherwise
        ``None``. Failure modes (any one falls through to the original
        fallback chain):
          - COMPREHENSIVE_DATA_MARKET_PROFILE_ENABLED flag is off.
          - Agent is not DB-bound (``_agent_is_db_bound`` is False).
          - No ``DATA_PRODUCING_TOOLS`` results in tool history.
          - Synthesis LLM call times out or errors.

    Implementation notes:
      - Wraps ``_call_synthesis_llm`` in ``asyncio.wait_for`` so a
        stuck LLM cannot block the user for >timeout_s.
      - Uses no ``tools`` payload (the synthesis call is text-only).
      - Tight prompt: ``max_tokens=6144`` (LLM_SYNTH_MAX_TOKENS default),
        temperature 0.7, top-of-prompt: directive reminder + 8-dim +
        format-specific instructions. Forces prose; refuses tool calls.
    """
    if not _research_directive_enabled():
        return None
    if not agent_name:
        return None
    # DB-bound gating — only fire on agents that the directive targets.
    try:
        from app.services.agent_prompts import _agent_is_db_bound
        if not _agent_is_db_bound(agent_name, agent_app):
            return None
    except Exception:
        return None
    # Need at least one data-producing tool call with results.
    has_data = False
    for tc in (tool_calls_for_frontend or []):
        if tc.get("name") not in DATA_PRODUCING_TOOLS:
            continue
        results = tc.get("results")
        if not isinstance(results, dict):
            continue
        if results.get("rows"):
            has_data = True
            break
        if results.get("report_card_payload"):
            has_data = True
            break
    if not has_data:
        return None
    # Build a compact data summary for the LLM prompt.
    summary = _data_summary_for_synthesis(tool_calls_for_frontend, max_chars=max_data_chars)
    if not summary.strip():
        return None

    sys_prompt = (
        "You are an expert research analyst (institutional-grade, not "
        "summary level). The previous LLM call emitted empty content "
        "instead of synthesizing — write the analysis now. REQUIRED "
        "STRUCTURE: (1) Section 1 Overview Dashboard — total items + "
        "sentiment tally + 1-paragraph macro; (2) Section 2 Executive "
        "Summary — ≤120 words + 2-3 actionable recommendations with "
        "trigger levels + risk alerts; (3) Section 3 Entity-by-Entity "
        "Deep Dive — for each distinct entity in the data: Snapshot "
        "(current value + Δ%) + 100-word Market Analysis + Forecast "
        "Table (near-term AND medium-term Baseline/Upside/Downside) + "
        "AI Decision (Strategy, Basis, 2-3 Key Risks with triggers); "
        "(4) Section 4 Disclaimer (AI-generated, not investment "
        "advice). STYLE: NO vague language — replace 'demand is stable' "
        "with specifics + percentages. NO marketing language. "
        "Quantified predictions only. Confidence calibration explicit. "
        "Risk symmetry (upside AND downside). NO tool calls. NO "
        "follow-up offers. Return ONLY the analysis."
    )
    user_prompt = (
        f"USER QUESTION:\n{(user_content or '(empty)')[:500]}\n\n"
        f"DATA SUMMARY (the agent already ran these queries):\n"
        f"{summary[:max_data_chars]}\n\n"
        "Write the institutional-grade analysis NOW. Do not preface, "
        "do not apologize, do not summarize what you will do — "
        "deliver the analysis."
    )

    try:
        import asyncio
        messages = [{"role": "user", "content": user_prompt}]
        result = await asyncio.wait_for(
            _call_synthesis_llm(sys_prompt, messages, endpoint=endpoint),
            timeout=timeout_s,
        )
        text = (result.get("content") or "").strip() if isinstance(result, dict) else ""
        if not text:
            return None
        # Light sanity guard: refuse the synthesis if it's < 80 chars
        # (the LLM emitted a literal "ok" or token noise instead of analysis).
        if len(text) < 80:
            logger.info(
                "_research_analyst_fallback: synthesis too short (%d chars) — falling through",
                len(text),
            )
            return None
        logger.info(
            "_research_analyst_fallback: produced %d-char analysis for agent=%s",
            len(text), agent_name,
        )
        return text
    except Exception as exc:
        logger.warning("_research_analyst_fallback: synthesis failed: %s", exc)
        return None


def _data_summary_for_synthesis(
    tool_calls_for_frontend: list[dict],
    max_chars: int = 6000,
) -> str:
    """Compact text representation of data-producing tool results, sized
    for an LLM prompt. Truncates individual sections at ~50% of budget
    to leave room for the directive in ``sys_prompt``.

    Returns a multi-section string ``"- tool_name: <summary>"``,
    suitable for inclusion in the user_prompt of ``_call_synthesis_llm``.
    """
    sections: list[str] = []
    for tc in (tool_calls_for_frontend or []):
        if tc.get("name") not in DATA_PRODUCING_TOOLS:
            continue
        results = tc.get("results")
        if not isinstance(results, dict):
            continue
        rows = results.get("rows")
        if not rows and not results.get("report_card_payload"):
            continue
        name = tc.get("name", "?")
        src = results.get("source_name") or "data source"
        if isinstance(rows, list) and rows:
            rows_summary = _build_column_summary(rows[:30])
            sections.append(
                f"- {name}({src}): {len(rows)} rows\n  {rows_summary}"
            )
        elif results.get("report_card_payload"):
            rcp = results["report_card_payload"]
            title = rcp.get("title") or "report"
            sections.append(f"- {name}({src}): report_card '{title}'")
        if sum(len(s) for s in sections) > max_chars:
            break
    return "\n".join(sections)


def _research_directive_enabled() -> bool:
    """Module-local flag check (used by ``_research_analyst_fallback`` and
    any future code that wants to honor the same opt-in). Wraps
    ``settings.getattr`` so tests can monkeypatch without touching the
    Pydantic Settings instance.
    """
    return bool(
        getattr(settings, "COMPREHENSIVE_DATA_MARKET_PROFILE_ENABLED", False)
    )


def _build_column_summary(rows: list[dict]) -> str:
    """Build a short text summary of columns and their value ranges.

    Used inside the forced-synthesis LLM prompt so the model has concrete
    numbers to write about. This does NOT generate the final answer — the
    LLM does that.
    """
    if not rows:
        return "(no rows)"
    sample = rows[:20]
    cols = list(sample[0].keys()) if sample else []
    lines: list[str] = []
    for c in cols[:12]:  # cap at 12 columns
        vals = [r.get(c) for r in sample if r.get(c) is not None]
        if not vals:
            continue
        # Try numeric summary
        nums = []
        for v in vals:
            try:
                nums.append(float(v))
            except (ValueError, TypeError):
                pass
        if nums:
            lines.append(
                f"  {c}: min={min(nums):.2f}, max={max(nums):.2f}, "
                f"sum={sum(nums):.2f} ({len(nums)} values)"
            )
        else:
            uniq = set(str(v) for v in vals[:8])
            lines.append(f"  {c}: {', '.join(str(u) for u in list(uniq)[:5])}")
    return "\n".join(lines) if lines else "(no numeric columns found)"


async def _force_llm_synthesis(
    user_msg: str,
    rows: list[dict],
    synth_call_fn,
    conversation_history: list[dict],
    max_retries: int = 2,
    endpoint=None,
) -> str:
    """Force the LLM to write a comprehensive analysis using the data rows.

    This replaces the old hardcoded _build_data_driven_answer. The LLM is
    essential because:
    - Different users have different databases (ERP, CRM, HR, etc.)
    - Different schemas need domain-specific interpretation
    - Only the LLM can understand business context from column names
    - A hardcoded builder would produce generic, wrong answers

    Args:
        user_msg: The user's original question
        rows: The data rows from the database
        synth_call_fn: Async callable that takes (prompt, history) → dict
        conversation_history: The conversation messages for context
        max_retries: How many forced attempts (default 2)

    Returns:
        The LLM's analysis text, or "" if all attempts fail
    """
    import time as _time
    _synth_t0 = _time.monotonic()
    logger.info(
        "DIAG _force_llm_synthesis entry: rows=%d, endpoint=%s",
        len(rows) if rows else 0,
        getattr(endpoint, "model_id", "?") if endpoint else "None",
    )
    if not rows:
        logger.warning("DIAG _force_llm_synthesis: rows empty, returning '' immediately")
        return ""

    col_summary = _build_column_summary(rows)
    # 2026-08-26: pass ALL the data to the LLM (not just 8 rows + 2000 chars)
    # so it can produce a real analysis based on the full dataset, not a
    # tiny sample.  We cap the JSON size to ~50KB to stay within token
    # limits, but keep the top-N rows sorted by relevance.
    # For datasets > 200 rows, include the first 200 + the pre-aggregated
    # summary block so the LLM has both individual records and totals.
    _MAX_ROWS_IN_PROMPT = 200
    _MAX_JSON_CHARS = 50_000
    if len(rows) <= _MAX_ROWS_IN_PROMPT:
        rows_data = json.dumps(rows, default=str, ensure_ascii=False)
        # If still too large, fall back to first N that fit
        if len(rows_data) > _MAX_JSON_CHARS:
            # Shrink by trimming row by row until under cap
            for n in range(len(rows), 0, -10):
                rows_data = json.dumps(rows[:n], default=str, ensure_ascii=False)
                if len(rows_data) <= _MAX_JSON_CHARS:
                    break
    else:
        # More than 200 rows: include first 200 + signal that the
        # pre-aggregated block is the authoritative source of truth
        rows_data = json.dumps(rows[:_MAX_ROWS_IN_PROMPT], default=str, ensure_ascii=False)
    rows_sample = rows_data  # alias for backward-compat in existing prompt builders

    # ── Pre-aggregation (P0) ───────────────────────────────────────────
    # Compute aggregates from ALL rows so the LLM has concrete numbers to
    # interpret instead of doing mental arithmetic on a tiny sample.
    preagg_block = ""
    try:
        from app.services.synexia.pre_aggregation import pre_aggregate

        preagg = pre_aggregate(rows)
        preagg_block = preagg.to_prompt_block()
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("Pre-aggregation failed in force_llm_synthesis: %s", e)
        preagg_block = ""

    for attempt in range(max_retries):
        # 2026-08-27: CEO-grade 5-section prompt used for ALL models
        # (was qwen3-local-only; the generic prompt below is now the retry
        # fallback, keeping quality high on first attempt for deepseek etc.)
        prompt = _build_ceo_synthesis_prompt(
            user_question=user_msg,
            data_rows=rows,
            columns=_columns,
            table_name="query_result",
        )
        prompt += (
            f"\n\nADDITIONAL CONTEXT (use these to compute specific numbers):\n"
            f"{preagg_block}\n\n"
            f"Column value ranges:\n{col_summary}\n\n"
            f"Data sample (first {min(len(rows), _MAX_ROWS_IN_PROMPT)} rows):\n{rows_sample}\n\n"
            f"REMINDER: You MUST produce ALL 5 sections in order: "
            f"Executive Summary, Key Metrics, Detailed Breakdown, "
            f"Risks & Opportunities, Recommended Actions. Do NOT stop "
            f"after the Executive Summary — the user expects a complete "
            f"report with all sections filled in."
        )
        if attempt > 0:
            # Retry fallback: even more explicit "previous attempt failed"
            # prompt so the model cannot dodge the report.
            prompt = (
                "IMPORTANT: Your previous attempt did not produce a real "
                "analysis. You MUST write actual content now.\n\n"
                f"The user asked: \"{user_msg[:200]}\"\n\n"
                f"You have {len(rows)} rows of real data. "
                "Write a 400-800 word report. DO NOT describe the data structure. "
                "DO NOT say 'I have completed' or 'Here is the artifact'. "
                "DO NOT stop after the executive summary. "
                "Compute real numbers FROM the data and include them. "
                "Use the 5 sections: Executive Summary, Key Numbers, Trends & "
                "Comparisons, Notable Anomalies, Recommended Next Steps.\n\n"
                f"=== PRE-AGGREGATES (all {len(rows)} rows) ===\n"
                f"{preagg_block}\n\n"
                f"=== COLUMN SUMMARY ===\n{col_summary}\n\n"
                f"=== DATA (first {min(len(rows), _MAX_ROWS_IN_PROMPT)} rows) ===\n{rows_sample}\n\n"
                "Start your answer directly with the Executive Summary. No preamble."
            )

        try:
            # Wrap synthesis LLM call in a timeout so a hung endpoint
            # doesn't make the user wait forever. 60s gives big models
            # (qwen3.6-27b local vLLM, deepseek, etc.) room to produce a
            # full CEO-grade 5-section report (30-50s typical).
            _SYNTH_TIMEOUT_S = 60.0
            result = await asyncio.wait_for(
                synth_call_fn(prompt, conversation_history),
                timeout=_SYNTH_TIMEOUT_S,
            )
            text = (result.get("content", "") or "").strip()
            # Check it's a real answer, not another placeholder
            # 2026-08-26: lowered threshold from 100 to 200 chars — a
            # 400-800 word report is at least 2000 chars, so 200 chars is
            # the bare minimum for any meaningful prose. (Was too lax.)
            _is_bad = (
                not text
                or len(text) < 200
                or text.startswith("I've completed your request")
                or text.startswith("Here is the artifact")
                or text.startswith("The data has been retrieved")
                or _APOLOGY_PATTERN_RE.search(text)
                or _BOUNCE_BACK_PATTERN_RE.search(text)
            )
            if not _is_bad:
                _elapsed = _time.monotonic() - _synth_t0
                logger.info(
                    "DIAG _force_llm_synthesis SUCCESS: attempt %d returned "
                    "%d chars in %.1fs (rows=%d)",
                    attempt + 1, len(text), _elapsed, len(rows),
                )
                return text
            logger.info(
                "force_llm_synthesis attempt %d produced bad answer "
                "(len=%d, apology=%s), retrying",
                attempt + 1, len(text),
                bool(_APOLOGY_PATTERN_RE.search(text)),
            )
        except Exception as e:
            _elapsed = _time.monotonic() - _synth_t0
            logger.warning(
                "DIAG _force_llm_synthesis attempt %d EXCEPTION after %.1fs "
                "(rows=%d): %s — caller should use deterministic fallback",
                attempt + 1, _elapsed, len(rows), e,
            )
            logger.warning(
                "force_llm_synthesis attempt %d failed: %s",
                attempt + 1, e,
            )

    _elapsed = _time.monotonic() - _synth_t0
    logger.warning(
        "DIAG _force_llm_synthesis: ALL %d ATTEMPTS FAILED, total elapsed %.1fs, "
        "rows=%d, returning '' (caller will use deterministic fallback)",
        max_retries, _elapsed, len(rows),
    )
    return ""  # all attempts failed


def _resolve_skill_for_synthesis(
    tool_calls_for_frontend: list[dict] | None,
    selected_skill: dict | None,
    selected_skill_id: str | None,
    db,
) -> tuple[str | None, str | None]:
    """Return (skill_name, skill_methodology) for the synthesis LLM.

    Priority:
      1. Latest successful ``load_skill_body`` tool observation (reflects
         the skill the LLM actually loaded this turn).
      2. DB lookup by ``selected_skill_id`` (when no observation yet —
         e.g. early synthesis or skill loaded via system-prompt injection).
      3. (None, None) — no skill context.

    Mirrors the pattern in capability_router.py:689-692.
    """
    # 1. Check tool observations for load_skill_body
    if tool_calls_for_frontend:
        for tc in reversed(tool_calls_for_frontend):
            if tc.get("name") == "load_skill_body" and tc.get("result"):
                res = tc["result"]
                if isinstance(res, dict):
                    name = res.get("name")
                    body = res.get("body")
                    if name and body:
                        return name, body[:5000]

    # 2. DB lookup by selected_skill_id
    if selected_skill_id and db:
        try:
            from app.models.tool import Tool
            tool_row = db.query(Tool).filter(Tool.id == selected_skill_id).first()
            if tool_row and tool_row.skill_md:
                name = (tool_row.name
                        or (selected_skill or {}).get("name", ""))
                return name, tool_row.skill_md[:5000]
        except Exception:
            pass  # Best-effort

    return None, None


def _has_data_rows(tool_calls_for_frontend: list[dict]) -> bool:
    """True if any data-producing tool call returned actual business rows
    this turn. Metadata-only rows (MIN_DATE/MAX_DATE/ENTRY_COUNT) don't
    count — they indicate the query returned schema info, not data."""
    from app.services.goal_contract import is_metadata_only_rows  # lazy
    for tc in (tool_calls_for_frontend or []):
        if tc.get("name") not in DATA_PRODUCING_TOOLS:
            continue
        result = tc.get("results")
        if isinstance(result, dict) and result.get("rows"):
            rows = result["rows"]
            if isinstance(rows, list) and len(rows) > 0 and is_metadata_only_rows(rows):
                continue  # metadata-only → not real data
            return True
    return False


def _schema_edge_count(result) -> int:
    """Best-effort count of high-confidence join edges in a describe_schema result.

    Returns 0 unless the result came from the schema-graph path and its
    rendered context lists related-table edges (lines shaped like
    ``    - <target_table> via <src> -> <tgt> (FK, conf=0.90)``).
    Only edges with confidence >= 0.8 count (those are the join edges the
    protocol block invites the agent to auto-join on).
    """
    if not isinstance(result, dict) or result.get("source") != "schema_graph":
        return 0
    schema_text = str(result.get("schema") or "")
    edges = 0
    for line in schema_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        m = re.search(r"conf=(\d\.\d{2})", stripped)
        if m and float(m.group(1)) >= 0.8:
            edges += 1
    return edges


def _is_dashboard_request(user_content: str) -> bool:
    """Heuristic: does the user's message ask for a dashboard?"""
    if not user_content:
        return False
    # Goal-Contract mode: the typo-tolerant normalizer is the single source
    # of truth (catches dashboard / Dashbord / dahsboard / 看板 / 仪表盘 /
    # 仪表板 / 数据面板 / 数据看板 / 大屏). Flag-off keeps the legacy path.
    if getattr(settings, "GOAL_CONTRACT_ENABLED", False):
        from app.services.goal_contract import normalize_deliverable_intent

        return normalize_deliverable_intent(user_content) == "dashboard"
    low = user_content.lower()
    if any(kw in low for kw in ("dashboard", "看板", "仪表盘", "数据面板")):
        return True
    # Flag-gated fuzzy "Dashbord"-class typo detection (no-op when the flag
    # is off, so behavior is unchanged by default).
    return fuzzy_dashboard_request(user_content)


# Strong-phrase anchors (EN + ZH) that mark a user message as automation-
# SETUP intent (NOT data-query intent).  When matched, the chat router
# rebinds the conversation to ``automation_agent`` so the LLM uses
# ``create_automation`` / ``update_automation`` instead of mistakenly
# calling ``ask_data_agent`` on the description text (see 2026-08-25
# third-pass regression).
_AUTOMATION_SETUP_HEADER_RE = re.compile(
    r"(?imx)"
    r"("
    # EN: explicit header line, with optional whitespace / casing.
    r"create\s+a?\s*new\s+automation\s+task"
    r"|set\s+up\s+(?:a\s+|an\s+)?(?:new\s+)?automation"
    r"|schedule\s+(?:a\s+|an\s+)?(?:new\s+)?(?:daily|hourly|weekly|cron)\s+task"
    r"|new\s+automation\s+task"
    # ZH: fullwidth colon, common colon variants and ZH verb choices.
    r"|帮我新建一个自动化任务|帮我创建一个自动化任务|新建自动化任务"
    r"|新建一个自动化任务|创建自动化任务|创建.{0,6}自动化任务"
    r"|添加自动化任务|设置.{0,4}定时任务|添加定时任务"
    r")"
)
# Structural signature of an automation-setup spec: Name/名称 +
# Schedule/调度 + Output format/输出格式 lines present together. Catches
# templates the user pastes without an explicit header (and any header
# typos the regex above misses).
_AUTOMATION_SETUP_STRUCTURE_RE = re.compile(
    r"(?isx)"
    r"(?:^|\n)\s*[-\u2022\u2043\u204c\u204d]?\s*(?:name|名称)\s*[:：]"
    r".*\n"
    r".*(?:schedule|调度(?:规则)?|频率)\s*[:：]"
    r".*\n"
    r".*(?:output[\s_]*format|输出格式)\s*[:：]"
)

# Header anchors for AGENT creation (rebind target: agent_builder).
# Narrower than the automation header so "create a new automation task"
# cannot accidentally match here.
_AGENT_CREATION_HEADER_RE = re.compile(
    r"(?imx)"
    r"("
    # EN: the noun MUST be "agent" — no other word fits this slot.
    r"create\s+(?:a\s+|an\s+|new\s+|me\s+|custom\s+|the\s+)*(?:AI\s+|intelligent\s+|new\s+)*agent"
    r"|build\s+(?:me\s+|a\s+|an\s+|new\s+|custom\s+)*(?:AI\s+|intelligent\s+|new\s+)*agent"
    r"|make\s+(?:me\s+|a\s+|an\s+|new\s+)*(?:AI\s+|intelligent\s+|new\s+)*agent"
    r"|set\s+up\s+(?:a\s+|an\s+|new\s+|custom\s+)*(?:AI\s+|intelligent\s+|new\s+)*agent"
    r"|design\s+(?:a\s+|an\s+|new\s+|custom\s+)*(?:AI\s+|intelligent\s+|new\s+)*agent"
    r"|new\s+agent"
    # ZH — ``(?:[一]?\s*个?)?`` covers "新建智能体" (no counter) and
    # "新建一个智能体" / "新建一个AI智能体" (counter word present).
    r"|新建\s*(?:[一]?\s*个?\s*)?(?:AI\s*|新\s*|自定义\s*)*智能体"
    r"|创建\s*(?:[一]?\s*个?\s*)?(?:AI\s*|新\s*|自定义\s*)*智能体"
    r"|帮我创建\s*(?:[一]?\s*个?\s*)?(?:AI\s*|新\s*|自定义\s*)*智能体"
    r"|做\s*一个?\s*(?:AI\s*|新\s*|自定义\s*)*智能体"
    r"|构建\s*(?:[一]?\s*个?\s*)?(?:AI\s*|新\s*|自定义\s*)*智能体"
    r"|设计\s*(?:[一]?\s*个?\s*)?(?:AI\s*|新\s*|自定义\s*)*智能体"
    r"|添加\s*(?:[一]?\s*个?\s*)?(?:AI\s*|新\s*)*智能体"
    r")"
)

# Header anchors for SKILL creation (rebind target: skill_agent).
_SKILL_CREATION_HEADER_RE = re.compile(
    r"(?imx)"
    r"("
    # EN: noun MUST be "skill" — disjoint from automation and agent headers.
    r"create\s+(?:a\s+|new\s+|custom\s+|reusable\s+|the\s+)*skill"
    r"|build\s+(?:me\s+|a\s+|new\s+|custom\s+|reusable\s+)*skill"
    r"|make\s+(?:me\s+|a\s+|new\s+|custom\s+|reusable\s+)*skill"
    r"|set\s+up\s+(?:a\s+|new\s+|custom\s+|reusable\s+)*skill"
    r"|design\s+(?:a\s+|new\s+|custom\s+|reusable\s+)*skill"
    r"|write\s+(?:a\s+|the\s+|new\s+|custom\s+)?SKILL\.md"
    r"|write\s+(?:a\s+|new\s+|custom\s+|reusable\s+)*skill"
    r"|add\s+(?:a\s+|new\s+|custom\s+|reusable\s+)*skill"
    r"|new\s+skill\b"
    # ZH — counter word ``一个`` is common in ZH; allow it.
    r"|新建\s*(?:[一]?\s*个?\s*)?(?:新\s*|可复用\s*|自定义\s*)*技能"
    r"|创建\s*(?:[一]?\s*个?\s*)?(?:新\s*|可复用\s*|自定义\s*)*技能"
    r"|做\s*一个?\s*(?:新\s*|可复用\s*|自定义\s*)*技能"
    r"|构建\s*(?:[一]?\s*个?\s*)?(?:新\s*|可复用\s*|自定义\s*)*技能"
    r"|写\s*(?:[一]?\s*个?\s*)?(?:新\s*|可复用\s*)*技能"
    r"|添加\s*(?:[一]?\s*个?\s*)?(?:新\s*|可复用\s*)*技能"
    r")"
)

# Intent table — order matters: most-specific match wins. The automation
# header is the most distinctive ("automation task" pattern), so it goes
# first; agent-creation and skill-creation headers are distinguished by
# the noun ("agent" vs "skill") and don't overlap with each other.
#
# Each row: (intent_name, target_agent, header_regex, structure_regex_or_None)
_SYSTEM_AGENT_INTENT_TABLE: list[tuple[str, str, "re.Pattern[str]", "re.Pattern[str] | None"]] = [
    (
        "automation_setup",
        "automation_agent",
        _AUTOMATION_SETUP_HEADER_RE,
        _AUTOMATION_SETUP_STRUCTURE_RE,
    ),
    (
        "agent_creation",
        "agent_builder",
        _AGENT_CREATION_HEADER_RE,
        None,
    ),
    (
        "skill_creation",
        "skill_agent",
        _SKILL_CREATION_HEADER_RE,
        None,
    ),
]


def _detect_system_agent_intent(
    user_content: str | None,
) -> tuple[str, str] | None:
    """Return ``(intent_name, target_agent_name)`` when ``user_content``
    is configuring a dedicated system agent, else ``None``.

    The detector scans a small priority-ordered table of header +
    (optional) structure regexes. Each entry corresponds to a system
    agent whose dedicated purpose (automation setup / agent creation /
    skill creation) is missing from the runtime project agent's
    toolset. The router rebinds the conversation so the LLM uses the
    right tool (``create_automation`` / agent-creation / skill-creator)
    instead of misfiring on ``ask_data_agent``.

    First match wins. The table is intentionally narrow — false
    positives just route the user to a clarifying prompt; false
    negatives leave the agent without the right tool (the 2026-08-25
    regressions). See ``tests/test_automation_setup_intent.py`` and
    ``tests/test_system_agent_intent_routing.py``.
    """
    if not isinstance(user_content, str) or not user_content.strip():
        return None
    for intent_name, target_agent, header_re, structure_re in _SYSTEM_AGENT_INTENT_TABLE:
        if header_re.search(user_content):
            return (intent_name, target_agent)
        if structure_re is not None and structure_re.search(user_content):
            return (intent_name, target_agent)
    return None


def _route_to_dedicated_system_agent(
    conv,
    user_content: str,
    user_role: str,
    db,
) -> tuple[str, str] | None:
    """Rebind ``conv.agent_name`` to the system agent that owns the
    detected intent. Persists so subsequent turns in the same chat stay
    routed there. Returns ``(intent_name, target_agent)`` on a rebind,
    else ``None``.

    No-op for non-user messages, when the conversation is already on
    the target agent, or when no intent matches. Best-effort on the
    ``commit`` — if persistence fails, the in-memory rebind is rolled
    back so the current turn still runs on the original agent.
    """
    if user_role != "user":
        return None
    detected = _detect_system_agent_intent(user_content)
    if detected is None:
        return None
    intent_name, target_agent = detected
    if getattr(conv, "agent_name", None) == target_agent:
        return None
    previous = conv.agent_name
    conv.agent_name = target_agent
    conv.updated_date = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception as exc:  # noqa: BLE001 — best-effort persist
        logger.warning(
            "system-agent routing: failed to persist rebind "
            "(intent=%s conv=%s previous=%s target=%s): %s",
            intent_name, getattr(conv, "id", "?"), previous,
            target_agent, exc,
        )
        db.rollback()
        conv.agent_name = previous
        return None
    logger.info(
        "system-agent routing: intent=%s rebind conv=%s %s -> %s "
        "(preview: %.80r)",
        intent_name, getattr(conv, "id", "?"), previous, target_agent,
        user_content,
    )
    return (intent_name, target_agent)


# ---------------------------------------------------------------------------
# Backward-compatible shims (kept so older tests / callers don't break).
# New code should call ``_detect_system_agent_intent`` /
# ``_route_to_dedicated_system_agent`` directly.
# ---------------------------------------------------------------------------


def _detect_automation_setup_intent(user_content: str | None) -> bool:
    """Deprecated: use ``_detect_system_agent_intent`` and check the
    intent name. Kept for backward compatibility — returns True iff
    the detected intent is ``automation_setup``.
    """
    detected = _detect_system_agent_intent(user_content)
    return bool(detected) and detected[0] == "automation_setup"


def _apply_automation_setup_routing(
    conv,
    user_content: str,
    user_role: str,
    db,
) -> bool:
    """Deprecated: use ``_route_to_dedicated_system_agent``. Returns
    True iff a rebind happened AND it was to ``automation_agent``.
    """
    result = _route_to_dedicated_system_agent(
        conv, user_content, user_role, db
    )
    return bool(result) and result[1] == "automation_agent"


def _tool_was_called(tool_calls_for_frontend: list[dict], tool_name: str) -> bool:
    """Check whether a specific tool was called during this turn."""
    return any(tc.get("name") == tool_name for tc in tool_calls_for_frontend)


# Retry hints that indicate an earlier ask_data_agent result was superseded
# by a re-query in the same turn (Fix 2a).
_ASK_DATA_RETRY_HINT_RE = re.compile(
    r"\b(retry|re-query|rerun|重试|重新|再查|重新查询)\b", re.IGNORECASE,
)


def _mark_superseded_ask_data_priors(
    tool_calls_for_frontend: list[dict],
    current_result: dict,
    inter_call_text: str,
) -> None:
    """Mark earlier ask_data_agent results as superseded (Fix 2a).

    A prior ask_data_agent result is superseded when:
      1. it returned no rows (or errored) AND the current result targets the
         same bound KB (matching ``source_id``/``kb_id``); or
      2. the assistant text between the two calls contains retry hints.

    Superseded results are excluded from deck/card generation so a stale
    empty/error query never contaminates the artifact's data or citations.
    """
    if not isinstance(current_result, dict):
        return
    current_kb = current_result.get("source_id") or current_result.get("kb_id")
    has_retry_hint = bool(_ASK_DATA_RETRY_HINT_RE.search(inter_call_text or ""))
    for tc in tool_calls_for_frontend:
        if tc.get("name") != "ask_data_agent":
            continue
        if tc.get("__superseded"):
            continue
        res = tc.get("results") or {}
        if not isinstance(res, dict):
            continue
        rows = res.get("rows")
        prior_empty = not isinstance(rows, list) or not rows
        prior_error = bool(res.get("error")) or res.get("success") is False
        if not (prior_empty or prior_error):
            continue  # prior returned real data — keep it
        prior_kb = res.get("source_id") or res.get("kb_id")
        if current_kb and prior_kb and current_kb != prior_kb:
            continue  # different bound KB — not superseded
        if prior_empty or prior_error:
            tc["__superseded"] = True
            tc.setdefault("results", {})["__superseded_note"] = (
                "superseded by a later ask_data_agent result in this turn"
            )
        elif has_retry_hint:
            tc["__superseded"] = True
            tc.setdefault("results", {})["__superseded_note"] = (
                "superseded by an explicit retry between calls"
            )


def _strip_trailing_pending(text: str, pending_sentence: str) -> str:
    """Remove the trailing sentence containing a pending-action phrase (Fix 4).

    Mirrors ``goal_contract.pending_action_phrase``'s sentence split so the
    sentence dropped is exactly the one the exit check flagged. Returns the
    original text when nothing matches (the caller then decides whether the
    limitation note is still warranted). When the pending sentence is the
    whole reply, an empty string is returned — the caller's appended note
    becomes the entire closing statement.
    """
    if not text or not pending_sentence:
        return text
    stripped = (text or "").strip()
    sentences = re.split(r"(?<=[.!?。！？])\s+", stripped)
    if not sentences or not sentences[-1].strip():
        return text
    if pending_sentence.strip() not in sentences[-1]:
        return text
    return " ".join(s.strip() for s in sentences[:-1] if s.strip())


# ── Post-loop internal-reference hygiene (Bug 3 fix) ────────────────────
# The final bubble must not reference internal loop iterations the user
# never saw: "the discrepancy", "you're right", "as I mentioned earlier",
# "let me re-query...", "I'll double-check...". These are trailing
# promises/regrets of verification — deterministic strip, no LLM.
_INTERNAL_REFERENCE_SENTENCE_RE = re.compile(
    r"(?:"
    r"the\s+discrepancy|"
    r"you['’]?re\s+right|you\s+are\s+right|"
    r"as\s+I\s+(?:mentioned|said|noted|discussed|explained)\s+earlier|"
    # "query" (bare) added 2026-08-21: the live traces' narration was
    # "Let me query the warehouse…" — the old re[- ]?query alternation
    # only caught "re-query", so the promise narration survived.
    r"let\s+me\s+(?:verify|re[- ]?query|query|double[- ]?check|check|look\s+into|"
    r"get\s+back|pull|run|fetch|retry|fix|correct|resolve|re[- ]?run)|"
    r"I['’]?ll\s+(?:re[- ]?query|double[- ]?check|verify|get\s+back|retry)|"
    r"I\s+will\s+(?:re[- ]?query|double[- ]?check|verify|retry)|"
    # ── Chinese internal-reference residue ─────────────────────────────
    r"让我\s*(?:再|重新)?\s*(?:核实|查询|查|确认|验证|检查|看看|试|重试)|"
    r"我再\s*(?:核实|查询|查|确认|验证|检查|看看|试|重试)|"
    r"(?:核实|复核|重查|重新查询|重新查|再查|确认一下|验证一下|检查一下)|"
    r"数据差异|"
    r"(?:我|刚才|前面|之前)(?:说|提)(?:过|到)|"
    r"你(?:说|讲)?得?对|"
    r"(?:我)?(?:回头|稍后)(?:再|继续|我)?"
    r")",
    re.IGNORECASE,
)


def _strip_internal_references(text: str) -> str:
    """Deterministically strip TRAILING sentences that reference internal
    loop iterations the user never saw (Bug 3 fix).

    Applied to ``accumulated_content`` post-loop, AFTER the promise-strip.
    Keeps stripping while the trailing sentence matches an internal-reference
    pattern, then trims dangling connectors. Never raises; on degenerate
    input the original text is returned unchanged.
    """
    if not text or not text.strip():
        return text
    stripped = (text or "").strip()
    # Split on English punctuation + whitespace AND Chinese sentence-ending
    # punctuation followed directly by the next sentence (no whitespace in
    # CJK text). Zero-width lookarounds keep the delimiters attached.
    sentences = re.split(r"(?<=[.!?。！？])\s+|(?<=[。！？])(?=\S)", stripped)
    while sentences and _INTERNAL_REFERENCE_SENTENCE_RE.search(sentences[-1]):
        sentences.pop()
    if not sentences:
        # Everything was internal-reference narration. Return "" — the
        # post-loop empty-bubble guarantee (2026-08-21) replaces the empty
        # result with a real fallback message, so we no longer keep the
        # original narration as a "remnant" (it leaked promise text into
        # the final bubble in the failing traces).
        return ""
    result = " ".join(s.strip() for s in sentences if s.strip())
    # Trim dangling connectors left behind by a removed sentence.
    result = re.sub(r"\s+(?:and|but|so|because|however|then|also)\s*$", "", result)
    result = re.sub(r"\s+[—\-–:;，,]\s*$", "", result)
    return result.strip()


# ── SQL / plan-narration strip (2026-08-21) ────────────────────────────
# The model sometimes narrates the SQL it intends to run (fenced blocks or
# bare SELECT paragraphs) and emits an empty "JSON Report Card" section —
# internal artifacts the user must never see in the final bubble.
_SQL_FENCE_RE = re.compile(
    r"```(?:sql)?\s*\n\s*(?:SELECT|WITH)\b.*?```",
    re.IGNORECASE | re.DOTALL,
)
_JSON_SECTION_HEADING_RE = re.compile(
    r"^[ \t]{0,3}#{1,6}[ \t]+(?:JSON\s+Report\s+Card|Report\s+Card\s+JSON|"
    r"Raw\s+JSON|JSON\s+Payload)[ \t]*$",
    re.IGNORECASE,
)


def _strip_sql_narration(text: str) -> str:
    """Deterministically remove SQL plan-narration artifacts from assistant
    prose ("leaking SQL data before final answer", 2026-08-21).

    Removes: (1) fenced ``sql`` / SELECT-leading code fences, (2) paragraphs
    that are bare SQL statements, (3) "JSON Report Card"-style markdown
    sections (heading plus a body of only fences/blank/brace lines).
    Never raises; degenerate input is returned unchanged.
    """
    if not text or not text.strip():
        return text
    # 1) Fenced SQL blocks.
    out = _SQL_FENCE_RE.sub("", text)
    # 2) Bare-SQL paragraphs (split on blank lines; drop SQL statements).
    paragraphs = re.split(r"\n\s*\n", out)
    kept = []
    for para in paragraphs:
        stripped_para = para.strip()
        if (
            stripped_para
            and re.match(r"^(?:SELECT|WITH)\b", stripped_para, re.IGNORECASE)
            and re.search(r"\bFROM\b", stripped_para, re.IGNORECASE)
        ):
            continue  # bare SQL statement — drop
        kept.append(para)
    out = "\n\n".join(kept)
    # 3) "JSON Report Card"-style sections: skip the heading and any body
    #    that is only fences / blank lines / brace JSON — stop skipping at
    #    the next heading or at real prose (never eat legitimate content).
    lines = out.split("\n")
    result_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if _JSON_SECTION_HEADING_RE.match(line):
            i += 1
            while i < len(lines):
                nxt = lines[i]
                if re.match(r"^[ \t]{0,3}#{1,6}[ \t]+", nxt):
                    break  # next heading ends the section
                if nxt.strip().startswith("```"):
                    i += 1
                    while i < len(lines) and not lines[i].strip().startswith("```"):
                        i += 1
                    i += 1  # past the closing fence
                    continue
                if not nxt.strip() or nxt.lstrip().startswith(("{", "[")):
                    i += 1
                    continue
                break  # real prose — section body ended before it
            continue
        result_lines.append(line)
        i += 1
    out = "\n".join(result_lines)
    # Collapse 3+ consecutive newlines left by removals.
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _compact_report_card(rcp: dict) -> str:
    """Build a compact LLM-facing digest of a data-agent report card.

    Extracts title, summary, up to 6 KPIs, chart metadata (title/type/row
    count + one sample row), and the top 3 insights.  This is far smaller
    than the raw 5-10k-token report_card_payload, so the final synthesis
    LLM keeps its output budget for prose instead of spending it on context.
    """
    lines: list[str] = []
    title = str(rcp.get("title") or "Report")
    lines.append(f"[DATA-AGENT REPORT: {title}]")
    summary = rcp.get("summary")
    if summary:
        lines.append(f"Summary: {str(summary)[:400]}")
    kpis = rcp.get("kpis") or []
    if kpis:
        parts = []
        for k in kpis[:6]:
            if isinstance(k, dict):
                label = k.get("label") or k.get("name") or "?"
                value = k.get("value") or ""
                parts.append(f"{label}={value}")
            else:
                parts.append(str(k))
        if parts:
            suffix = " (…)" if len(kpis) > 6 else ""
            lines.append("KPIs: " + "; ".join(parts) + suffix)
    chart = rcp.get("chart")
    if isinstance(chart, dict):
        c_title = chart.get("title") or title
        c_type = chart.get("type") or "bar"
        lines.append(f"Chart: {c_title} ({c_type})")
        data = chart.get("data")
        if isinstance(data, list) and data:
            lines.append(f"Chart rows: {len(data)}")
            if isinstance(data[0], dict):
                try:
                    lines.append(
                        "Sample row: "
                        + json.dumps(data[0], ensure_ascii=False)[:300]
                    )
                except Exception:
                    pass
    insights = rcp.get("insights") or []
    if insights:
        lines.append("Insights (top 3):")
        for ins in insights[:3]:
            txt = ins.get("text") if isinstance(ins, dict) else ins
            lines.append(f"- {str(txt)[:300]}")
    actions = rcp.get("actions") or []
    if actions:
        lines.append(f"Follow-up actions available: {len(actions)}")
    return "\n".join(lines)


def _condense_data_agent_results(
    llm_messages: list[dict],
    max_tool_result_chars: int = 6000,
) -> None:
    """Condense oversized ``ask_data_agent`` tool results in-place (Fix D).

    Data-agent results embed a ``report_card_payload`` (KPIs, chart series,
    insights) that can be 5-10k tokens per result.  When two or more such
    results are in context at once, the final synthesis LLM (typically
    DeepSeek) can exceed its output budget and return empty content — the
    ``assistant_content``-empty fallback then fires.

    Called only on the final synthesis iteration (``tool_choice == "none"``)
    so mid-loop tool decisions still see full data.  Each oversized
    data-agent tool result is replaced with a compact digest produced by
    ``_compact_report_card``.  Only the LLM-facing context is touched: the
    full payload remains in ``tool_calls_for_frontend`` (frontend report-card
    rendering) and in the persisted conversation messages.
    """
    for msg in llm_messages:
        if msg.get("role") != "tool":
            continue
        content = msg.get("content")
        if not isinstance(content, str) or len(content) <= max_tool_result_chars:
            continue
        try:
            data = json.loads(content)
        except (TypeError, ValueError):
            continue  # not JSON — leave untouched
        if not isinstance(data, dict):
            continue
        rcp = data.get("report_card_payload")
        if not isinstance(rcp, dict):
            continue  # not a data-agent result — leave untouched
        try:
            msg["content"] = _compact_report_card(rcp)
        except Exception as exc:  # defensive: never break the loop over context
            logger.warning("condense report card failed: %s", exc)


def _empty_answer_needs_force(
    assistant_content: str,
    content_streamed: bool,
    accumulated_prose: list,
    forces_used: int,
    has_usable_data: bool,
) -> bool:
    """Fix 2: the model ended the turn with no prose at all (or only
    promise narration like "Let me verify..."), but usable data was
    retrieved — force ONE re-synthesis pass instead of letting the
    generic empty-content fallback fire.

    The check now also fires when accumulated_prose contains ONLY
    pending-action phrases (promise text), since that's not a real
    answer — the model announced an action but never executed it.
    """
    if forces_used >= 1 or not has_usable_data:
        return False
    # When the model's FINAL reply is empty and usable data exists, force
    # synthesis regardless of content_streamed or accumulated prose. The
    # streamed text from earlier iterations was intermediate narration
    # (promises, "let me check…"), not a real answer. The user expects a
    # synthesized final response, not a collection of partial narrations.
    if not assistant_content:
        return True
    return False


def _build_selected_skill_runtime_block(db: Session, selected_skill: dict | None, selected_skill_id: str | None) -> str:
    """Build a hard runtime instruction block for a chat-selected skill."""
    if not selected_skill and not selected_skill_id:
        return ""
    skill_id = selected_skill_id or (selected_skill or {}).get("id")
    skill_name = (selected_skill or {}).get("name") or "selected skill"
    description = (selected_skill or {}).get("description") or ""
    trigger = (selected_skill or {}).get("trigger") or ""
    skill_md = ""
    try:
        from app.models.tool import Tool
        query = db.query(Tool).filter(Tool.is_deleted == False, Tool.enabled == True)  # noqa: E712
        tool = query.filter(Tool.id == skill_id).first() if skill_id else query.filter(Tool.name == skill_name).first()
        if tool:
            skill_name = tool.name or skill_name
            description = tool.description or description
            trigger = tool.trigger or trigger
            skill_md = tool.skill_md or ""
    except Exception as exc:
        logger.debug("selected skill runtime lookup failed (non-fatal): %s", exc)

    body = skill_md[:8000]
    return (
        "\n\n<selected_runtime_skill>\n"
        "The user explicitly selected this skill in the chat composer. This selection has highest priority.\n"
        f"Skill id: {skill_id or 'n/a'}\n"
        f"Skill name: {skill_name}\n"
        f"Trigger: {trigger}\n"
        f"Description: {description}\n"
        "Rules:\n"
        "- Follow this skill's SKILL.md methodology, output structure, tone, and validation expectations.\n"
        "- Do not substitute a generic report flow when this selected skill is relevant.\n"
        "- Use bound data tools only to gather facts required by the skill; then produce the final output in the skill's format.\n"
        "- Do not call unavailable legacy weekly-report tools.\n"
        + (f"\nSKILL.md:\n{body}\n" if body else "\nSKILL.md: not available; use the metadata above.\n")
        + "</selected_runtime_skill>\n"
    )

# Data-producing tools whose row payloads feed the deferred deliverable
# pipeline (purpose tagging + contract dataset collection). The agent may
# collect data via ask_data_agent (delegated) OR via direct SQL tools
# (execute_query / execute_sql / sql_query); forecast_brief returns rows
# for market reads. Anything not listed here is skipped by the structural
# `result.get("rows")` gate anyway — the set only widens classification.
#
# ``fetch_data_batch`` (the direct-parallel-SQL fast path used by
# ``automation_runtime_agent`` and cron-triggered report runs) returns
# ``{"success": True, "results": [{"label": ..., "rows": [...], ...}, ...]}``
# — the row array is NESTED inside each sub-query result, NOT at the top
# level. ``_data_rows_fallback`` handles both shapes; we add it here so the
# post-loop pipeline (and the v3 empty-bubble guarantee) treats its
# results as ground-truth data instead of falling back to the generic
# apology when the runtime turn emits empty content. Pinned by
# tests/test_fetch_data_batch_data_fallback.py.
DATA_PRODUCING_TOOLS: frozenset = frozenset({
    "ask_data_agent",
    "execute_query",
    "execute_sql",
    "sql_query",
    "forecast_brief",
    "fetch_data_batch",
    # Institutional-grade research-analyst pipeline (2026-08-25)
    # comprehensive_data/profile="market" emits structured report_card
    # payloads; collect_enterprise_data emits the ExecutiveReport card.
    # Both must be treated as data producers so the LLM-floor fallback
    # recognizes their results as ground-truth data to synthesize on.
    "comprehensive_data",
    "collect_enterprise_data",
})

# FIX 2026-08-24: sub-agent tools that return a `prompt_text` formatted
# answer (not rows). Without harvesting their text, market-report turns
# collapse to the generic "I gathered some information" fallback because
# the row pipeline ignores them entirely.
TEXT_PRODUCING_TOOLS: frozenset = frozenset({
    "ask_decision",
    "ask_forecast_pricing",
})


def _harvest_text_answer(result: dict) -> str | None:
    """Extract a formatted text answer from a market-research sub-agent."""
    if not isinstance(result, dict):
        return None
    for key in ("prompt_text", "answer", "summary", "narrative"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# Per-tool overrides for the loop guard. ``memory`` is idempotent-write —
# one identical call is already a loop. ``ask_data_agent`` is capped per turn
# by name. For report creation, repeated serial data-agent calls are the
# dominant latency source; one comprehensive query should gather the full
# dataset, and a second call is the hard stop.
TOOL_CALL_CAPS: dict[str, int] = {
    "memory": 1,
    "ask_data_agent": 6,
    "fetch_data_batch": 3,  # direct SQL fast path; each call can have up to 8 queries
    # Defense-in-depth against the interrupt leak: if an agent re-enables the
    # interrupt tool via tool_config, cap it at 2 calls/turn so it can never
    # eat the budget the way `interrupt(action=check)` polling did.
    "interrupt": 2,
    # clarify is a UX handoff tool; repeated calls signal a stuck agent.
    "clarify": 3,
}
_NAME_ONLY_KEYED_TOOLS: set[str] = {"ask_data_agent", "fetch_data_batch"}

# Stream-then-buffer kill-switch (Gap 1). When True, the v3 agentic loop
# streams LLM tokens to the client as they arrive via ``_stream_llm_with_tools``
# instead of awaiting the buffered ``_call_llm_with_tools`` and emitting one
# big delta at the end. Set to False (or override via env) to instantly
# revert to the legacy buffered path if a provider misbehaves under streaming.
STREAM_TOKEN_DELTAS = os.environ.get("ZHANLU_STREAM_TOKEN_DELTAS", "1") not in ("0", "false", "False")

# SSE keepalive interval (seconds). Long LLM/tool stretches can leave the
# event stream silent for minutes; proxies (nginx, ingress, Cloudflare) kill
# idle connections well before that, which the client surfaces as
# "[Stream error: network error]". A comment ping every N seconds keeps the
# connection alive without affecting the event protocol.
#
# Bug 3 fix: default reduced from 15s → 5s. Some aggressive proxies
# (Cloudflare free tier, AWS ALB default, corporate reverse proxies) idle-
# kill connections in 30-60s. A 5s heartbeat resets every proxy timer
# we've seen in the wild and is still low enough to not waste bandwidth.
# Override via the ZHANLU_SSE_HEARTBEAT_S env var for tuning.
_SSE_HEARTBEAT_INTERVAL = max(1.0, float(os.environ.get("ZHANLU_SSE_HEARTBEAT_S", "5")))

# Ceiling for the post-tool experience / response-cache hook. get_embedding()
# (LLM API call) inside _store_turn_cache can take 30-60s; running it on the
# event loop starved the SSE heartbeat and made the frontend see "connection
# interrupted" before the `done` frame was emitted (2026-08-19). Offloaded to
# a worker thread; anything slower than this ceiling is logged and skipped so
# the `done` frame always fires.
EXPERIENCE_HOOK_TIMEOUT_S = float(os.environ.get("ZHANLU_EXPERIENCE_HOOK_TIMEOUT_S", "60"))

# ── tool_progress heartbeat during long tool execution ──────────────────
# Delegation / sub-agent tools (ask_perception, ask_intelligence,
# ask_diagnosis, ask_perception_intelligence_diagnosis, ask_data_agent, …)
# can run for minutes. While they execute, the v3 loop emits
# ``tool_progress`` SSE frames so the client sees liveness and proxies
# don't idle-kill the connection. Fast utility tools are excluded.
_LONG_RUNNING_TOOLS = frozenset({
    "ask_data_agent", "ask_weekly_report",
    "ask_forecast", "ask_report",
    "ask_knowledge_graph", "ask_decision", "ask_macro_override",
    "web_search", "execute_automation", "execute_code",
    # Artifact / dashboard generation can take tens of seconds to minutes
    # (Jinja scaffold + DB writes + optional git commits + poller start).
    # Register them so the batch tool wrapper emits tool_progress frames and
    # long builds never idle-kill the SSE connection.
    "create_artifact", "create_fullstack_dashboard",
    "update_fullstack_dashboard", "create_dashboard",
    "revert_fullstack_dashboard", "run_sandbox_skill",
})


def _is_long_running_tool(tool_name: str) -> bool:
    """True if ``tool_name`` may run for tens of seconds or minutes."""
    return is_long_running_tool(tool_name, _LONG_RUNNING_TOOLS)


# ── Agent fast mode (2026-08-25, universalized 2026-08-27) ────────────────
# Originally gated to qwen3-local vLLM; proven to cut turn time 200s → 42-52s
# while keeping answer quality (8-15 LLM calls/turn → 3-5). Now applied to
# ALL models — big cloud models (deepseek, etc.) are equally capable and
# faster, so the same lean loop produces smoother, faster, high-quality
# answers. No per-model hardcoding.

def _effective_max_tool_iterations(endpoint) -> int:
    """Return the effective MAX_TOOL_ITERATIONS for the user's model."""
    if settings.AGENT_FAST_MODE_ENABLED:
        return settings.AGENT_FAST_MAX_TOOL_ITERATIONS
    return MAX_TOOL_ITERATIONS


def _effective_goal_contract_enabled(endpoint) -> bool:
    """Return whether goal-contract is enabled for the user's model."""
    if settings.AGENT_FAST_MODE_ENABLED:
        return settings.AGENT_FAST_GOAL_CONTRACT_ENABLED
    return settings.GOAL_CONTRACT_ENABLED


def _effective_verify_nudge_max(endpoint) -> int:
    """Return the effective VERIFY_NUDGE_MAX for the user's model."""
    if settings.AGENT_FAST_MODE_ENABLED:
        return settings.AGENT_FAST_VERIFY_NUDGE_MAX
    return settings.VERIFY_NUDGE_MAX


def _effective_data_agent_budget_seconds(endpoint) -> float:
    """Return the effective DATA_AGENT_BUDGET_SECONDS for the user's model."""
    if settings.AGENT_FAST_MODE_ENABLED:
        return settings.AGENT_FAST_DATA_AGENT_BUDGET_SECONDS
    from app.services.tool_handlers.delegation_tools import DATA_AGENT_BUDGET_SECONDS
    return DATA_AGENT_BUDGET_SECONDS


def _effective_self_eval_max_replans(endpoint) -> int:
    """Return the effective SELF_EVAL_MAX_REPLANS for the user's model."""
    if settings.AGENT_FAST_MODE_ENABLED:
        return settings.AGENT_FAST_SELF_EVAL_MAX_REPLANS
    return settings.SELF_EVAL_MAX_REPLANS


def _effective_self_eval_timeout(endpoint) -> float:
    """Return the effective SELF_EVAL_LLM_GATE_TIMEOUT_S for the user's model."""
    if settings.AGENT_FAST_MODE_ENABLED:
        return settings.AGENT_FAST_SELF_EVAL_LLM_GATE_TIMEOUT_S
    return settings.SELF_EVAL_LLM_GATE_TIMEOUT_S


def _effective_force_synthesis_max_retries(endpoint) -> int:
    """Return the effective max_retries for _force_llm_synthesis."""
    if settings.AGENT_FAST_MODE_ENABLED:
        return settings.AGENT_FAST_FORCE_SYNTHESIS_MAX_RETRIES
    return 1  # legacy default


def _effective_synthesis_max_tokens(endpoint) -> int:
    """Return the effective synthesis max_tokens for the user's model."""
    if settings.AGENT_FAST_MODE_ENABLED:
        return settings.AGENT_FAST_SYNTHESIS_MAX_TOKENS
    return 1536  # legacy default


# ── Component 4: CEO-grade synthesis prompt (all models) ─────────────────

_CEO_SYNTHESIS_TEMPLATE = """\
You are a CEO-grade business analyst. Using ONLY the data below, write a \
comprehensive answer in this exact structure:

## Executive Summary
[2-3 sentences: what was asked, what was found, the headline number]

## Key Metrics
- Total {measure_label}: {currency_symbol}{total:,.2f}
- Date range covered: {date_range}
- Number of records: {row_count}
- Top {dimension_label}: {top_dimension_value}

## Detailed Breakdown
[markdown table of top 10-20 rows, or bullets if many dimensions]

## Risks & Opportunities
[1-2 sentences: notable patterns, anomalies, trends]

## Recommended Actions
[1-2 actionable items based on the data]

DATA (use only this, do not invent numbers):
{data_inline}
"""

_MAX_INLINE_ROWS = 100
_MAX_INLINE_CHARS = 4000


def _build_ceo_synthesis_prompt(
    user_question: str,
    data_rows: list,
    columns: list,
    table_name: str = "data",
) -> str:
    """Build the CEO-grade 5-section synthesis prompt (all models).

    Inlines up to 100 rows / 4000 chars of data directly in the prompt so
    the model doesn't have to "look up" the data from a tool result.
    """
    from collections import Counter

    total_rows = len(data_rows) if data_rows else 0
    truncated = (data_rows or [])[:_MAX_INLINE_ROWS]
    # Build markdown table
    if truncated and columns:
        header = "| " + " | ".join(columns) + " |"
        sep = "| " + " | ".join("---" for _ in columns) + " |"
        body_lines = []
        for row in truncated:
            vals = []
            for c in columns:
                v = row.get(c, "") if isinstance(row, dict) else ""
                if isinstance(v, float):
                    vals.append(f"{v:,.2f}")
                else:
                    vals.append(str(v))
            body_lines.append("| " + " | ".join(vals) + " |")
        data_inline = "\n".join([header, sep] + body_lines)
    else:
        data_inline = "(no rows)"

    if total_rows > _MAX_INLINE_ROWS:
        data_inline += f"\n\n*Showing first {_MAX_INLINE_ROWS} of {total_rows} rows.*"

    # Truncate to max chars
    if len(data_inline) > _MAX_INLINE_CHARS:
        data_inline = data_inline[:_MAX_INLINE_CHARS] + "\n...(truncated)"

    # Compute simple metrics for the template placeholders
    measure_label = "value"
    currency_symbol = "¥"
    total = 0.0
    # Find first numeric column for "total"
    for row in truncated:
        if not isinstance(row, dict):
            continue
        for c in columns:
            v = row.get(c)
            if isinstance(v, (int, float)):
                total += float(v)
                measure_label = c
                break

    # Top dimension (first non-numeric column)
    dimension_label = "item"
    top_dimension_value = "n/a"
    for c in columns:
        if c == measure_label:
            continue
        dimension_label = c
        vals = [str(row.get(c, "")) if isinstance(row, dict) else "" for row in truncated]
        if vals:
            top_dimension_value = Counter(vals).most_common(1)[0][0]
        break

    # Date range
    date_range = "n/a"
    for c in columns:
        if "date" in c.lower() or "time" in c.lower():
            dates = sorted([str(row.get(c, "")) if isinstance(row, dict) else "" for row in truncated if row.get(c) if isinstance(row, dict)])
            if dates:
                date_range = f"{dates[0]} to {dates[-1]}"
            break

    return _CEO_SYNTHESIS_TEMPLATE.format(
        measure_label=measure_label,
        currency_symbol=currency_symbol,
        total=total,
        date_range=date_range,
        row_count=total_rows,
        dimension_label=dimension_label,
        top_dimension_value=top_dimension_value,
        data_inline=data_inline,
    )


# ── Component 5: Deterministic fallback (never "Sorry") ─────────────────────

_APOLOGY_PATTERNS = [
    "i couldn't", "i couldn't find", "let me re-query", "let me try again",
    "i wasn't able to", "unable to retrieve", "no data found",
    "i apologize", "i'm sorry", "sorry, i hit an error",
]


def _should_trigger_fallback(synthesis_text: str, data_rows: list) -> bool:
    """Return True if the deterministic fallback should fire.

    Fires when:
    - synthesis_text is empty AND data_rows is non-empty
    - synthesis_text matches an apology pattern AND data_rows is non-empty
    - synthesis_text is <100 chars AND data_rows is non-empty
    Does NOT fire when data_rows is empty (no data to show).
    """
    if not data_rows:
        return False
    if not synthesis_text or not synthesis_text.strip():
        return True
    if len(synthesis_text.strip()) < 100:
        return True
    lower = synthesis_text.lower()
    for pattern in _APOLOGY_PATTERNS:
        if pattern in lower:
            return True
    return False


def _build_deterministic_fallback(
    data_rows: list,
    columns: list,
    table_name: str = "data",
) -> str:
    """Build a deterministic markdown summary from data rows (no LLM).

    Includes:
    - Numeric summary (total/avg/min/max) for numeric columns
    - Top-3 values for categorical columns
    - Date range for date columns
    - Markdown table of top 20 rows
    """
    from collections import Counter

    if not data_rows:
        return "No data available."

    total_rows = len(data_rows)
    lines = ["## Executive Summary (auto-generated)", ""]

    # Detect column types
    numeric_cols = []
    categorical_cols = []
    date_cols = []
    for c in columns:
        sample_val = next((row.get(c) for row in data_rows if isinstance(row, dict) and row.get(c) is not None), None)
        if isinstance(sample_val, (int, float)):
            numeric_cols.append(c)
        elif isinstance(sample_val, str):
            if any(k in c.lower() for k in ["date", "time", "created", "updated"]):
                date_cols.append(c)
            else:
                categorical_cols.append(c)

    # Numeric summary
    for c in numeric_cols:
        vals = [float(row.get(c, 0)) for row in data_rows if isinstance(row, dict) and isinstance(row.get(c), (int, float))]
        if vals:
            total = sum(vals)
            avg = total / len(vals)
            mn = min(vals)
            mx = max(vals)
            lines.append(f"- Total {c}: ¥{total:,.2f}")
            lines.append(f"- Average {c}: ¥{avg:,.2f}")
            lines.append(f"- Min {c}: ¥{mn:,.2f}")
            lines.append(f"- Max {c}: ¥{mx:,.2f}")

    # Categorical top-3
    for c in categorical_cols[:3]:
        vals = [str(row.get(c, "")) for row in data_rows if isinstance(row, dict) and row.get(c)]
        if vals:
            top3 = Counter(vals).most_common(3)
            for val, count in top3:
                lines.append(f"- Top {c}: {val} ({count} records)")

    # Date range
    for c in date_cols:
        dates = sorted([str(row.get(c, "")) for row in data_rows if isinstance(row, dict) and row.get(c)])
        if dates:
            lines.append(f"- Date range ({c}): {dates[0]} to {dates[-1]}")

    lines.append(f"- Number of records: {total_rows}")
    lines.append("")

    # Markdown table (top 20 rows)
    lines.append("## Data Preview (top 20 rows)")
    lines.append("")
    display_rows = data_rows[:20]
    if display_rows and columns:
        header = "| " + " | ".join(columns) + " |"
        sep = "| " + " | ".join("---" for _ in columns) + " |"
        lines.append(header)
        lines.append(sep)
        for row in display_rows:
            vals = []
            for c in columns:
                v = row.get(c, "") if isinstance(row, dict) else ""
                if isinstance(v, float):
                    vals.append(f"{v:,.2f}")
                else:
                    vals.append(str(v))
            lines.append("| " + " | ".join(vals) + " |")

    if total_rows > 20:
        lines.append(f"\n*Showing first 20 of {total_rows} rows.*")

    lines.append("\n*Note: LLM synthesis unavailable. Showing raw data summary.*")
    return "\n".join(lines)


def _start_finalize_offloaded(db, finalize_kwargs: dict) -> asyncio.Task:
    """Run ``finalize_into_artifact`` off the event loop with heartbeat coverage.

    ``finalize_into_artifact`` calls ``run_sandbox_skill_sync`` which blocks
    the event loop for 30-120s while a Docker container renders PPTX/DOCX.
    Running it directly inside the SSE generator starves the 5s heartbeat and
    the browser shows "connection interrupted". This helper commits the caller's
    session, then spawns the work in a thread with its own ``SessionLocal`` so
    the event loop stays free for heartbeats.
    """
    from app.database import SessionLocal as _FinalizeSessionLocal

    # Make conversation/message rows visible to the thread's new session before
    # we hand off.
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise

    def _threaded_finalize():
        _fdb = _FinalizeSessionLocal()
        try:
            # FIX B (2026-08-22): keep attribute values resident after
            # commit so the returned Artifact row stays readable once this
            # session closes.  The default expire_on_commit=True expires
            # every attribute at commit; reading any field (e.g.
            # `artifact.id`) on the detached row then raises
            # DetachedInstanceError in the caller.
            _fdb.expire_on_commit = False
            result = finalize_into_artifact(_fdb, **finalize_kwargs)
            _fdb.commit()
            return result
        except Exception:
            _fdb.rollback()
            raise
        finally:
            _fdb.close()

    return asyncio.ensure_future(asyncio.to_thread(_threaded_finalize))


async def _emit_tool_progress_while_waiting(
    task: asyncio.Task,
    parsed_calls: list,
    interval: float = 5.0,
):
    """Yield ``tool_progress`` SSE frames while ``task`` is still running.

    The tool-execution task keeps running in the background; every
    ``interval`` seconds we yield a ``data: {...}`` chunk that marks the
    in-flight tool calls as ``status: running``. This is additive — the
    frontend already renders ``tool_progress`` events and treats entries
    without a ``results`` key as in-flight.
    """
    async for frame in emit_tool_progress_while_waiting(
        task,
        parsed_calls,
        interval=interval,
        display_names=TOOL_DISPLAY_NAMES,
    ):
        yield frame


async def _sse_with_heartbeat(agen, interval: float = _SSE_HEARTBEAT_INTERVAL):
    """Wrap an async generator of SSE frames with keepalive comment pings.

    Yields ``: ping`` comments whenever the wrapped generator produces no
    frame for ``interval`` seconds. SSE clients ignore comment lines, so
    this is protocol-safe; it only resets proxies' idle timers.
    """
    it = agen.__aiter__()
    pending: asyncio.Task | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(it.__anext__())
            done, _pending_set = await asyncio.wait({pending}, timeout=interval)
            if pending in done:
                try:
                    frame = pending.result()
                except StopAsyncIteration:
                    break
                pending = None
                yield frame
            else:
                yield ": ping\n\n"
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        try:
            await agen.aclose()
        except Exception:
            pass


async def _guarantee_done(agen):
    """Guarantee a final ``done`` SSE frame reaches the client.

    The v3 stream only commits the assistant message on the ``done``
    frame. Any unhandled exception between the report-card render and the
    done emission (DB error, serialization crash, provider hiccup) used to
    kill the generator without ``done`` → the frontend showed "Sorry, the
    connection was interrupted" even though the answer/report card had
    already rendered. Draining the inner generator here lets us emit a
    minimal ``done`` (with the generic fallback content) before closing
    cleanly, so the client always unblocks and never shows the
    interrupted error.

    Cancellation (client disconnect) and GeneratorExit (``aclose``) are
    re-raised untouched — the client is gone, emitting is pointless.
    """
    try:
        async for frame in agen:
            yield frame
    except (asyncio.CancelledError, GeneratorExit, StopAsyncIteration):
        raise
    except BaseException as exc:  # noqa: BLE001 — must never drop the client
        # exc_info=True (2026-08-21): the fully-empty-page failures left no
        # diagnosable trace — the message alone was not enough to locate the
        # dying frame. Full traceback from now on.
        logger.error(
            "v3 stream: unhandled generator error; emitting fallback done: %s",
            exc,
            exc_info=True,
        )
        try:
            yield (
                'data: ' + json.dumps({
                    "type": "done",
                    "content": _GENERIC_EMPTY_CONTENT_FALLBACK,
                    "trace": [],
                    "conversation": {},
                }) + '\n\n'
            )
        except Exception:
            pass


async def _disconnect_safe_stream(agen_factory):
    """Run the agent-loop SSE generator detached from the client connection.

    The v3 stream runs the WHOLE agent loop inside an async generator that
    is consumed by a StreamingResponse. When the SSE client disconnects
    (browser tab closed, remote-browser session recycled, proxy idle
    timeout), Starlette calls ``aclose()`` on the generator — the loop is
    cancelled mid-turn and the final assistant message is NEVER persisted
    (symptom: conversation frozen with an empty assistant reply, dashboard
    build silently dead, no ``done`` frame). The unstable remote browser
    recycled every few minutes, killing multi-step dashboard builds
    repeatedly.

    Fix: consume the generator in a background asyncio task that pushes
    SSE frames into a queue; this wrapper yields from the queue. When the
    client disconnects, the wrapper's ``finally`` does NOT cancel the pump
    task — the agent loop keeps running to completion, persists its final
    message (the loop owns its own DB session, see event_stream's
    stream_db shadow), and only then exits.

    ``agen_factory`` must be a zero-arg CALLABLE returning a fresh async
    generator (e.g. the ``event_stream`` / ``_fsm_event_stream`` closures)
    — the pump creates exactly one instance.
    """
    queue: asyncio.Queue = asyncio.Queue()
    _SENTINEL = object()
    pump_task: "asyncio.Task | None" = None

    async def _pump():
        agen = None
        try:
            agen = agen_factory()
            async for frame in agen:
                await queue.put(frame)
        except (GeneratorExit, asyncio.CancelledError):
            raise
        except BaseException as exc:  # noqa: BLE001 — surface loop failures as SSE error frames
            logger.error(
                "v3 stream: background agent-loop pump failed: %s",
                exc,
                exc_info=True,
            )
            try:
                await queue.put(
                    'data: ' + json.dumps({
                        "type": "error",
                        "message": f"Agent loop failed: {exc}",
                    }) + '\n\n'
                )
            except Exception:
                pass
        finally:
            if agen is not None:
                try:
                    await agen.aclose()
                except Exception:
                    pass
            await queue.put(_SENTINEL)

    try:
        pump_task = asyncio.create_task(_pump())
        while True:
            frame = await queue.get()
            if frame is _SENTINEL:
                break
            yield frame
    finally:
        # Client disconnect / response closed: stop streaming frames but
        # DO NOT cancel the pump task — the agent loop continues and
        # persists its final message in the background.
        if pump_task is not None and not pump_task.done():
            pass  # detached by design: the loop finishes on its own


def _estimate_ask_data_agent_cap(user_content: str) -> int:
    """Estimate how many ``ask_data_agent`` calls the query likely needs.

    Uses simple heuristics based on the number of distinct data concepts
    (sales, inventory, etc.) and metrics mentioned in the user message.

    Returns an integer between 2 and 8.

    Examples::

        "sales report"                     → 2
        "sales report (volume, revenue)"   → 3
        "volume, revenue, margin, inventory" → 5
        "sales + inventory + margin + cost"  → 6
    """
    import re as _re

    cap = 2  # minimum: even simple queries need at least 2

    # Count distinct data concept keywords
    _CONCEPT_PATTERNS = [
        r'(?i)sales|sale|销售|订单|出库|出货|volume|出货量',
        r'(?i)inventory|stock|库存|入库|存货|qty|数量',
        r'(?i)purchase|procurement|采购|进货',
        r'(?i)margin|profit|利润|毛利|毛利率',
        r'(?i)revenue|turnover|营收|收入|销售额',
        r'(?i)cost|expense|成本|费用|支出',
        r'(?i)payment|receivable|应收|应付|回款',
        r'(?i)production|manufacturing|生产|制造',
        r'(?i)logistics|shipping|物流|发货|运输',
    ]
    concepts_found = sum(
        1 for pat in _CONCEPT_PATTERNS if _re.search(pat, user_content)
    )
    # Each additional concept beyond the first adds 1 to cap
    cap += max(0, concepts_found - 1)

    # Count explicit metrics listed in parentheses
    # e.g., "(volume, revenue, margin, inventory)" → 4 metrics
    metrics_match = _re.search(r'\(([^)]+)\)', user_content)
    if metrics_match:
        metrics = [
            m.strip() for m in metrics_match.group(1).split(',')
            if m.strip() and not _re.match(r'(?i)\d{4}', m.strip())  # skip years
        ]
        if metrics:
            cap = max(cap, len(metrics) + 1)  # +1 for header/discovery

    # Also check for comma-separated or "/" separated metric-like items
    # outside parentheses: "volume, revenue, margin, inventory"
    _METRIC_WORDS = {
        'volume', 'revenue', 'margin', 'inventory', 'qty', 'quantity',
        'amount', 'cost', 'profit', 'turnover', 'sales',
        '销量', '营收', '利润', '库存', '毛利', '成本', '数量', '金额',
    }
    # Split on commas or "and" or "/" between 2+ word phrases
    _metric_candidates = _re.split(r'[,/]|\s+[Aa][Nn][Dd]\s+', user_content)
    _metric_count = sum(
        1 for mc in _metric_candidates
        if mc.strip().lower() in _METRIC_WORDS
    )
    if _metric_count >= 3:
        cap = max(cap, _metric_count + 1)

    # Count "and" / "/" separators between concept-like terms
    and_splits = _re.split(r'\s+[Aa][Nn][Dd]\s+|/', user_content)
    if len(and_splits) > 2:
        cap = max(cap, min(len(and_splits), 6))

    return min(cap, 8)  # hard ceiling


def _detect_tool_call_loop(
    llm_messages: list[dict],
    start_idx: int = 0,
    dynamic_caps: dict[str, int] | None = None,
) -> tuple[str, int] | None:
    """Return ``(tool_name, count)`` if a single tool+arguments
    combination has been called ``TOOL_CALL_HARD_CAP`` or more times in
    the scanned messages, else ``None``.

    Walks ``llm_messages[start_idx:]`` and counts every function call in
    assistant-role ``tool_calls`` lists. The KEY is ``(name,
    canonicalized_arguments)`` — not just ``name`` — so that
    legitimate investigation (the same tool called with different
    queries) does NOT trip the guard, while a true loop
    (same tool, same arguments, repeated) does.

    ``start_idx`` scopes the scan to the current turn (pass the index of
    the triggering user message). The default ``0`` scans the whole
    history — the original behavior kept for the v2 call sites. The v3
    stream turn-scopes the guard because counting earlier turns made
    legitimate cross-turn repetitions (e.g. re-running the same
    automation task once per day via "Run Now") trip the in-turn guard
    on iteration 0 of a fresh turn.

    This is a defense-in-depth check against the LLM getting stuck in
    a retry/refine loop on a single tool. The classic case is
    ``agent_builder`` looping on ``skills(action=load, name=foo)``
    while trying to load a missing skill. The classic NON-loop is
    the agent_builder searching skills with three different queries
    to find a relevant match — that must be allowed.
    """
    scanned = llm_messages[max(0, start_idx):]
    # First pass: pair tool_call_ids with their outcomes so the guard is
    # SUCCESS-AWARE — failed calls get one extra chance (cap + 1) for the
    # self-heal/reformulation path to recover, while successful (or
    # pending-approval, or still-in-progress) calls trip at ``cap``.
    outcomes: dict[str, bool] = {}
    for m in scanned:
        if m.get("role") != "tool":
            continue
        tid = m.get("tool_call_id")
        if not tid:
            continue
        try:
            payload = json.loads(m.get("content") or "{}")
        except (ValueError, TypeError):
            payload = {}
        ok = bool(payload.get("success", True)) or bool(payload.get("requires_approval"))
        outcomes[tid] = ok

    counts: dict[tuple[str, str], list[int]] = {}
    for m in scanned:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            func = tc.get("function") or {}
            name = func.get("name")
            if not name:
                continue
            if name in _NAME_ONLY_KEYED_TOOLS:
                key = (name, "")
            else:
                args_str = func.get("arguments") or ""
                # Canonicalize the arguments: try to parse as JSON and
                # re-serialize with sorted keys, so that {"a":1,"b":2} and
                # {"b":2,"a":1} are treated as the same call. Fall back to
                # the raw string on parse failure (defensive — the LLM
                # should always produce valid JSON for tool calls, but we
                # don't want a malformed arg to silently mask a real loop).
                try:
                    canonical = json.dumps(json.loads(args_str), sort_keys=True)
                except (ValueError, TypeError):
                    canonical = args_str
                key = (name, canonical)
            # In-progress calls (no tool result yet) default to success —
            # conservative: don't let an unfinished call mask a real loop.
            ok = outcomes.get(tc.get("id"), True)
            bucket = counts.setdefault(key, [0, 0])
            bucket[0 if ok else 1] += 1
    for (name, _), (succ, fail) in counts.items():
        # Dynamic caps (computed from user message complexity) override
        # the static TOOL_CALL_CAPS for specific tools like ask_data_agent.
        cap = (dynamic_caps or {}).get(name, TOOL_CALL_CAPS.get(name, TOOL_CALL_HARD_CAP))
        if succ >= cap or fail >= cap + 1:
            return name, succ + fail
    return None


def _persisted_result_str(
    tool_name: str,
    result: dict,
    conversation_id: str,
    *,
    context_window_tokens: int | None = None,
) -> str:
    """Serialize a tool result dict, applying Layer 2 (per-result) persistence.

    Large results are written to disk and replaced with an inline preview +
    pointer. Returns the JSON string to append to ``llm_messages``.
    """
    result_str = json.dumps(result, ensure_ascii=False, default=str)
    try:
        from app.services.compaction import get_context_window
        storage_dir = os.path.join(
            getattr(settings, "AGENT_WORKSPACE_DIR", "agent_workspace"),
            settings.TOOL_RESULT_STORAGE_DIR,
        )
        # The real, per-model context window (admin-set or auto-probed)
        # drives persistence budgets — NOT the model-name heuristic, so an
        # unknown/small-window model never overflows and a large one is
        # fully used.  Falls back to the heuristic when not available.
        _ctx_window = get_context_window(
            get_model(), context_window_tokens=context_window_tokens,
        )
        config = budget_for_context_window(_ctx_window)
        new_str, _meta = persist_tool_result(
            tool_name, result_str, storage_dir, config, conversation_id
        )
        return new_str
    except Exception as e:
        logger.debug("Tool result persistence failed (non-fatal): %s", e)
        return result_str


def _apply_turn_budget_to_messages(
    llm_messages: list[dict],
    batch_tool_call_ids: list[str],
    batch_tool_names: list[str],
    conversation_id: str,
    *,
    context_window_tokens: int | None = None,
) -> None:
    """Layer 3: if total tool output in this turn exceeds budget, spill largest.

    Scans the tool messages appended in this iteration (identified by
    ``batch_tool_call_ids``) and replaces the largest ones with disk-persisted
    previews if the aggregate size exceeds the turn budget.
    """
    try:
        from app.services.compaction import get_context_window
        # Collect (tool_call_id, tool_name, content) for this batch
        batch_contents: list[tuple[str, str, str]] = []
        for msg in llm_messages:
            if msg.get("role") != "tool":
                continue
            tid = msg.get("tool_call_id")
            if tid not in batch_tool_call_ids:
                continue
            idx = batch_tool_call_ids.index(tid)
            batch_contents.append((tid, batch_tool_names[idx], msg.get("content", "")))
        if not batch_contents:
            return

        total = sum(len(c) for _, _, c in batch_contents)
        config = budget_for_context_window(
            get_context_window(
                get_model(), context_window_tokens=context_window_tokens,
            )
        )
        if total <= config.turn_budget_chars:
            return

        # Sort by size descending -- spill largest first
        sorted_contents = sorted(batch_contents, key=lambda x: len(x[2]), reverse=True)
        storage_dir = os.path.join(
            getattr(settings, "AGENT_WORKSPACE_DIR", "agent_workspace"),
            settings.TOOL_RESULT_STORAGE_DIR,
        )
        current_total = total
        new_contents: dict[str, str] = {}
        for tid, tool_name, content in sorted_contents:
            if current_total <= config.turn_budget_chars:
                new_contents[tid] = content
                continue
            if tool_name in config.no_persist_tools:
                new_contents[tid] = content
                continue
            new_str, meta = persist_tool_result(
                tool_name, content, storage_dir, config, conversation_id, force=True
            )
            if meta["persisted"]:
                current_total -= len(content) - len(new_str)
                new_contents[tid] = new_str
            else:
                new_contents[tid] = content

        # Replace contents in llm_messages
        for msg in llm_messages:
            if msg.get("role") != "tool":
                continue
            tid = msg.get("tool_call_id")
            if tid in new_contents:
                msg["content"] = new_contents[tid]
    except Exception as e:
        logger.debug("Turn budget application failed (non-fatal): %s", e)


# ---------------------------------------------------------------------------
# ReAct reflexion — critique system message on tool failure.
#
# In the raw ReAct loop (the legacy chat path duplicated across add_message,
# resume_conversation, and add_message_stream), a failed tool call used to be
# fed back as a plain ``role:"tool"`` result and the LLM would often retry
# blindly — repeating the same malformed arguments or giving up entirely.
#
# Reflexion injects a *critique* system message after the failure so the next
# iteration explicitly reasons about *why* the call failed and proposes a
# corrected approach.  This mirrors the self-heal behavior of the FSM path
# (Phase 2) for the three raw-loop sites that lacked it.
#
# The critique is a statically-constructed system prompt (no extra LLM call),
# so it adds zero latency on the happy path and only fires when a tool result
# has ``success is False`` with a truthy ``error`` — never on successes,
# approval pauses, or empty-data results.
# ---------------------------------------------------------------------------

_REFLEXION_CRITIQUE_PREAMBLE = (
    "One or more tool calls just failed. Before retrying, reflect on each "
    "failure below: identify the most likely cause (wrong argument name or "
    "value, missing required parameter, type mismatch, resource not found, "
    "or permission issue). Then either retry with corrected arguments, "
    "switch to a more appropriate tool, or explain clearly to the user what "
    "went wrong and what they can do. Do NOT repeat the exact same call "
    "unchanged."
)


def _inject_reflexion_critique(
    llm_messages: list[dict],
    calls: list[dict],
    results: list,
) -> None:
    """Append a reflexion critique system message when any tool in the
    batch failed.

    MUST be called after every ``role:"tool"`` result message for the batch
    has been appended to *llm_messages* — OpenAI-compatible APIs require an
    assistant ``tool_calls`` message to be immediately followed by all of its
    tool results, so the system message must come *after* the complete result
    set, never interleaved.

    Only real failures (``success is False`` with a truthy ``error``) trigger
    the critique.  Approval pauses (``requires_approval``) and empty-data
    results are silently ignored.

    Mutates *llm_messages* in place; returns ``None``.
    """
    failures: list[tuple[str, str, str]] = []
    for call, result in zip(calls, results):
        if not isinstance(result, dict):
            continue
        if result.get("success") is False and result.get("error"):
            name = call.get("tool_name", "<unknown>")
            args_str = call.get("args_str", "")
            if len(args_str) > 200:
                args_str = args_str[:200] + "…"
            failures.append((name, args_str, str(result["error"])))
    if not failures:
        return
    lines = [_REFLEXION_CRITIQUE_PREAMBLE, ""]
    for name, args_str, error in failures:
        lines.append(f"  • {name}({args_str}) → {error}")
    # 2026-08-25: vLLM (qwen3.6-27b) rejects mid-list system messages with
    # HTTP 400 "System message must be at the beginning." The reflexion
    # critique is a mid-conversation nudge, so it must be role="user".
    # DeepSeek API tolerates mid-list system messages, so this change is
    # backward-compatible for the cloud path too.
    llm_messages.append({"role": "user", "content": "\n".join(lines)})
    logger.info(
        "ReAct reflexion: injected critique for %d failed tool call(s): %s",
        len(failures),
        ", ".join(name for name, _, _ in failures),
    )


# ---------------------------------------------------------------------------
# Internal tools — meta-operations the agent_builder (and other system agents)
# use for skill discovery / capability lookup. These are NOT user actions, so
# the chat UI collapses them into a one-line progress indicator instead of a
# noisy collapsible panel. The LLM still receives full tool results in
# llm_messages; only the frontend rendering is affected.
# ---------------------------------------------------------------------------

INTERNAL_TOOLS = frozenset({
    "skills",
    "skills_hub",
    "skills_sync",
    "skills_guard",
    "skill_manager",
    "skill_provenance",
    "skill_usage",
    "list_tools",
    "list_market_agents",
    "list_knowledge_bases",
})

INTERNAL_TOOL_LABELS = {
    "skills":               "Searching available capabilities...",
    "skills_hub":           "Searching available capabilities...",
    "skills_sync":          "Syncing skills...",
    "skills_guard":         "Checking skill safety...",
    "skill_manager":        "Managing skills...",
    "skill_provenance":     "Checking skill origin...",
    "skill_usage":          "Checking skill usage...",
    "list_tools":           "Searching available capabilities...",
    "list_market_agents":   "Searching available capabilities...",
    "list_knowledge_bases": "Searching available data sources...",
}

# ── Activity-step descriptions for inline Claude-style steps ───────────
# Maps tool names to human-readable descriptions for the `activity_step`
# SSE events emitted from `add_message_stream` (v3). The frontend renders
# these as numbered steps (① ② ③) inside the assistant message bubble.
ACTIVITY_STEP_DESCRIPTIONS = {
    # Discovery / capability tools
    "list_tools":           "Scanning available tools and capabilities",
    "search_skills":        "Searching for the {query} skills",
    "skills":               "Searching for relevant skills",
    "skills_hub":           "Searching for relevant skills",
    "list_market_agents":   "Browsing available agent templates",
    "list_knowledge_bases": "Checking available data sources",
    "skills_sync":          "Syncing skills",
    "skills_guard":         "Checking skill safety",
    "skill_manager":        "Managing skills",
    "skill_provenance":     "Checking skill origin",
    "skill_usage":          "Checking skill usage",

    # Core action tools
    "create_agent":         "Building the agent definition",
    "update_agent":         "Updating the agent configuration",
    "create_skill":         "Creating a new capability",
    "update_skill":         "Updating an existing capability",
    "create_automation":    "Setting up the automation task",
    "update_automation":    "Updating the automation task",

    # Data tools
    "ask_data_agent":       "Querying the bound data source",
    "describe_schema":      "Inspecting the database schema",
    "list_data_sources":    "Listing available data sources",
    "web_search":           "Searching the web for relevant information",
    "web_extract":          "Extracting content from web pages",
    "read_file":            "Reading the {path} file",
    "write_file":           "Writing to the {path} file",
    "execute_code":         "Running the provided code",
    "delegate_task":        "Delegating a subtask",
    "image_generation":     "Generating an image",
    "memory":               "Saving important context to memory",
    "todo":                 "Updating the task list",
    "run_sandbox_skill":    "Running the {skill} in sandbox",

    # Admin / infrastructure tools
    "update_env_config":    "Updating environment configuration",
    "docker_compose_restart": "Restarting Docker services",

    # Utility tools
    "url_safety":           "Checking URL safety",
    "fuzzy_match":          "Fuzzy-matching strings",
    "path_security":        "Validating file path security",
    "patch_parser":         "Parsing the patch content",
    "process_registry_list": "Listing running processes",
    "process_registry_tail": "Tailing process logs",
    "process_registry_kill": "Stopping a running process",

    # ── CAD (Fusion 360) tools ─────────────────────────────────────
    # Human-readable verbs so the live feed shows WHAT the agent is doing
    # (e.g. "Extruding the profile 4 mm (pos)") instead of generic
    # "Running tool: fusion360_extrude". Unfilled {placeholders} are
    # stripped by _format_activity_description when args don't carry them.
    "fusion360_ping":                "Pinging the Fusion 360 bridge",
    "fusion360_info":                "Inspecting the Fusion 360 scene",
    "fusion360_execute_python":      "Running Fusion 360 Python",
    "fusion360_lookup_api":          "Looking up the Fusion 360 API",
    "fusion360_sketch_create":       "Creating a sketch on the active plane",
    "fusion360_sketch_line":         "Drawing a sketch line",
    "fusion360_sketch_rectangle":    "Drawing a rectangle on the sketch",
    "fusion360_sketch_circle":       "Drawing a circle (r={radius_mm} mm)",
    "fusion360_sketch_polygon":      "Drawing a {sides}-sided polygon (r={circumradius_mm} mm)",
    "fusion360_sketch_arc":          "Drawing an arc",
    "fusion360_sketch_arc_3point":   "Drawing a 3-point arc",
    "fusion360_sketch_spline":       "Drawing a spline",
    "fusion360_extrude":             "Extruding the profile {distance_mm} mm ({direction})",
    "fusion360_revolve":             "Revolving the profile",
    "fusion360_loft":                "Lofting between profiles",
    "fusion360_sweep":               "Sweeping the profile",
    "fusion360_cylinder":            "Creating a cylinder ({diameter_mm} mm × {height_mm} mm)",
    "fusion360_sphere":              "Creating a sphere",
    "fusion360_torus":               "Creating a torus",
    "fusion360_coil":                "Creating a coil",
    "fusion360_shell":               "Shelling the body",
    "fusion360_hole":                "Drilling a hole",
    "fusion360_thread":              "Adding a thread",
    "fusion360_chamfer":             "Chamfering the body {distance_mm} mm",
    "fusion360_edge_chamfer":        "Chamfering edges {distance_mm} mm",
    "fusion360_fillet":              "Filleting edges",
    "fusion360_move":                "Moving body {body_index} by ({dx_mm}, {dy_mm}, {dz_mm}) mm",
    "fusion360_extend_face":         "Extending body {body_index} {distance_mm} mm ({face})",
    "fusion360_mirror":              "Mirroring the body",
    "fusion360_combine":             "Combining bodies",
    "fusion360_project":             "Projecting geometry onto the sketch",
    "fusion360_construction_plane":  "Adding a construction plane",
    "fusion360_rectangular_pattern": "Creating a rectangular pattern",
    "fusion360_circular_pattern":    "Creating a circular pattern",
    "fusion360_component":           "Creating a component",
    "fusion360_revolute_joint":      "Adding a revolute joint",
    "fusion360_rigid_joint":         "Adding a rigid joint",
    "fusion360_slider_joint":        "Adding a slider joint",
    "fusion360_joint_limits":        "Setting joint limits",
    "fusion360_probe":               "Probing the live geometry",
    "fusion360_verify_build":        "Verifying the build against the spec",
    "fusion360_measure":             "Measuring geometry",
    "fusion360_physical_properties": "Reading physical properties",
    "fusion360_export_geometry":     "Exporting geometry",
    "fusion360_import_dxf":          "Importing a DXF",
    "fusion360_make_drawing":        "Creating a drawing",
    "fusion360_save":                "Saving the design",
    "fusion360_user_parameter":      "Setting a user parameter",
}

# ── Helper: format an activity_step description with optional args ──────
# Named placeholders like {skill} / {path} / {query} are resolved from the
# tool-call args. (Legacy `_VAR_PATTERN = "{}"` made the resolver dead code:
# no template contains a literal empty-brace pair, so every template with a
# named placeholder early-returned unresolved — e.g. "Running the {skill}…".)
_PLACEHOLDER_RE = re.compile(r"\{\w+\}")

# Per-tool key priority: when filling a placeholder like {data_source_id},
# try these arg keys in order and use the first non-None non-empty value.
# Format: {tool_name: [(placeholder_name, [priority_arg_keys, ...]), ...]}
_ARG_KEY_PRIORITY: dict[str, list[tuple[str, list[str]]]] = {
    "ask_data_agent": [
        ("data_source_id", ["data_source_id", "question"]),
    ],
    "run_sandbox_skill": [
        ("skill", ["skill_name", "skill", "format"]),
    ],
}

def _format_activity_description(tool_name: str, args: dict | None = None) -> str:
    """Build a human-readable activity step description from tool name + args.

    Templates like ``"Searching for the {query} skills"`` are filled from
    ``args`` keys. Falls back to ``"Running tool: {tool_name}"`` when no
    mapping exists.

    When a tool has ``_ARG_KEY_PRIORITY`` entries, placeholders are resolved
    by trying each fallback key in priority order (first non-None non-empty
    value wins). This handles cases like ``ask_data_agent`` where the LLM
    may omit ``data_source_id`` (using the default source).
    """
    template = ACTIVITY_STEP_DESCRIPTIONS.get(tool_name)
    if not template:
        return f"Running tool: {tool_name}"
    if not args:
        # No args to fill named placeholders — never surface a literal
        # "{skill}" / "{path}" template; strip and collapse whitespace.
        return re.sub(r"\s{2,}", " ", _PLACEHOLDER_RE.sub("", template)).strip()
    if not _PLACEHOLDER_RE.search(template):
        return template
    try:
        priorities = _ARG_KEY_PRIORITY.get(tool_name, [])
        prio_map: dict[str, list[str]] = {ph: keys for ph, keys in priorities}
        result = template
        # Find all {placeholder} occurrences and resolve them
        placeholders = re.findall(r"\{(\w+)\}", result)
        for ph in placeholders:
            placeholder = "{" + ph + "}"
            value: str | None = None
            if ph in prio_map:
                # Try priority keys in order
                for key in prio_map[ph]:
                    val = args.get(key)
                    if val is not None and val != "":
                        value = str(val)
                        break
            else:
                # Direct match from args
                val = args.get(ph)
                if val is not None and val != "":
                    value = str(val)
            if value is not None:
                if len(value) > 40:
                    value = value[:37] + "..."
                result = result.replace(placeholder, value)
        # Strip any placeholders that could not be resolved from args so
        # the UI never shows a literal "{skill}" / "{path}" template.
        result = re.sub(r"\{\w+\}", "", result).strip()
        return result
    except Exception:
        return template


# ── Tool command / output summarizers (expandable step detail) ────────
# Arg keys that best answer "what did the tool run with", in priority
# order. First non-empty string wins.
_COMMAND_ARG_KEYS = ("code", "command", "path", "query", "url", "skill",
                     "file_path", "filename", "title", "question")
# Result keys that best answer "what came back", in priority order.
_OUTPUT_KEYS = ("output", "stdout", "text", "preview", "summary",
                "message", "content")
_MAX_COMMAND_CHARS = 600
_MAX_OUTPUT_CHARS = 500

# Regex to strip raw SQL/DB internals from displayed command text.
# Matches [Schema: ...] blocks and common ERP-style internal column IDs
# (F-prefixed uppercase identifiers common in ERP systems).
# NOTE: We intentionally do NOT strip generic table/column names since
# different databases use different naming conventions. Only strip clearly
# internal/technical identifiers that users wouldn't understand.
_SQL_HINT_PATTERN = re.compile(
    r"\[Schema:[^\]]*\]"            # [Schema: ...] blocks
    r"|\bF[A-Z][A-Z0-9_]+\b",      # F-prefixed internal IDs (common in ERP systems)
    re.IGNORECASE,
)


def _strip_sql_hints(text: str) -> str:
    """Remove raw SQL table/column names from a command string for display."""
    cleaned = _SQL_HINT_PATTERN.sub("", text)
    # Collapse whitespace left behind
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned


def _summarize_tool_command(tool_name: str, args: dict | None) -> str | None:
    """Compact, capped string of WHAT a tool was invoked with.

    For ``execute_code`` this is the code itself (Claude shows the bash
    block); for read/search tools it's the path/query; for everything
    else a compact JSON of the args. Returns None when there is nothing
    meaningful to show.
    """
    if not args or not isinstance(args, dict):
        return None
    for key in _COMMAND_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            val = val.strip()
            # For ask_data_agent, strip raw SQL/ERP internals before display
            if tool_name == "ask_data_agent":
                val = _strip_sql_hints(val)
            if len(val) > _MAX_COMMAND_CHARS:
                val = val[:_MAX_COMMAND_CHARS] + "…"
            return val
    try:
        compact = json.dumps(args, ensure_ascii=False, default=str)
    except Exception:
        return None
    if compact in ("{}", "null"):
        return None
    if len(compact) > _MAX_COMMAND_CHARS:
        compact = compact[:_MAX_COMMAND_CHARS] + "…"
    return compact


def _summarize_tool_output(result: dict | None) -> str | None:
    """Short human-readable preview of a tool result for expanded steps.

    On failure, surfaces the error. Otherwise pulls the first non-empty
    string from ``_OUTPUT_KEYS``. Never dumps the whole result dict —
    artifact-producing results can be huge.
    """
    if not isinstance(result, dict):
        return None
    if result.get("success") is False:
        err = result.get("error") or result.get("message")
        if isinstance(err, str) and err.strip():
            err = err.strip()
            return err if len(err) <= _MAX_OUTPUT_CHARS else err[:_MAX_OUTPUT_CHARS] + "…"
        return "Tool call failed"
    for key in _OUTPUT_KEYS:
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            val = val.strip()
            if len(val) > _MAX_OUTPUT_CHARS:
                val = val[:_MAX_OUTPUT_CHARS] + "…"
            return val
    return None


def _emit_fsm_state(state: str, detail: str | None = None) -> str:
    """Return an SSE ``fsm_state`` payload for a SynexiaFSM transition.

    Additive SSE event type (reserved by P0 plan in the protocol doc). The
    current frontend consumers filter on ``event.type`` and ignore unknown
    types, so emitting this in addition to the existing ``delta`` /
    ``tool_progress`` / ``done`` events is backward-compatible.

    Args:
        state: The FSM state name (e.g. ``"plan"``, ``"verify"``, ``"done"``).
        detail: Optional human-readable description of the transition.
    """
    payload: dict = {"type": "fsm_state", "state": state}
    if detail:
        payload["detail"] = detail
    return f'data: {json.dumps(payload)}\n\n'


def _emit_steer(messages: list[str]) -> str:
    """Return an SSE ``steer`` payload for mid-turn user steer messages (P2).

    Additive SSE event type — the existing frontend consumers filter on
    ``event.type`` and ignore unknown types, so this is backward-compatible.
    The new Chat.jsx consumer (P2.3) renders the messages inline as a small
    "steered: …" marker.

    Args:
        messages: The list of steer messages drained in this iteration, in
            FIFO order. Empty list is allowed (yields an empty steer event
            for symmetry, though the v3 region only yields when non-empty).
    """
    payload: dict = {"type": "steer", "messages": list(messages)}
    return f'data: {json.dumps(payload)}\n\n'


def _discard_steer(conversation_id: str) -> None:
    """Discard the per-conversation steer queue. Best-effort, never raises.

    Called from the v3 ``add_message_stream`` event_stream's exit paths
    (done / error / paused / resume) so the in-process queue does not leak
    across turns. Any exception is caught and logged at WARNING.
    """
    try:
        steer_bus.discard(conversation_id)
    except Exception as _discard_err:
        logger.warning(
            "v3 event_stream: steer discard failed for conv=%s (non-fatal): %s",
            conversation_id, _discard_err,
        )


# ---------------------------------------------------------------------------
# Decision-summary pause flow (R4)
# ---------------------------------------------------------------------------
# When the agent_builder emits a `:::decision-summary` block in its text
# response, we want to pause the loop BEFORE any create_agent tool call is
# executed and surface a structured review panel to the user. The flow is:
#
#   1) The router parses the block via ``parse_decision_summary_block``.
#   2) The parsed payload is stored in ``conv.metadata_["pending_agent_payload"]``
#      and ``conv.metadata_["awaiting_decision_summary"] = True``.
#   3) The assistant message is persisted (with the raw block stripped so the
#      UI never shows a literal ```:::decision-summary`` fence) and the loop
#      breaks. The frontend detects ``awaiting_decision_summary`` and renders
#      the DecisionSummaryCard.
#   4) When the user clicks Create Agent, the frontend POSTs the (possibly
#      edited) payload to ``POST /apps/{app_id}/agents/conversations/{id}/confirm-decision``
#      which executes the create_agent handler with the user-edited fields.
#
# This helper centralises the persistence logic so v2 main, v2 resume, and
# v3 stream all behave identically. It returns ``True`` when the decision
# summary was found and the pause flow was triggered, ``False`` otherwise.
#
# Allowed keys in the payload (anything else is ignored):
#   name, description, project, capabilities, model, agent_type, skills,
#   knowledge_bases, data_read, data_write, human_fallback, trace_enabled,
#   log_level, max_call_count, max_retries, max_iterations, status, plus
#   the five prompt_* layers.

_DECISION_SUMMARY_ALLOWED_KEYS: frozenset[str] = frozenset({
    "name", "description", "project",
    "capabilities", "skills", "knowledge_bases",
    "model", "agent_type",
    "prompt_identity", "prompt_boundary", "prompt_reasoning",
    "prompt_tools", "prompt_output",
    "data_read", "data_write", "human_fallback",
    "trace_enabled", "log_level",
    "max_call_count", "max_retries", "max_iterations",
    "temperature", "top_p", "max_tokens", "status",
    "topology",
})


def _sanitize_decision_payload(raw: dict) -> dict:
    """Filter a parsed decision-summary block down to the allowed keys
    and normalise obvious types. Never raises — bad values are dropped
    silently so the UI can render a partial draft without crashing.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k, v in raw.items():
        if k not in _DECISION_SUMMARY_ALLOWED_KEYS:
            continue
        # Normalise list-shaped values
        if k in ("capabilities", "skills", "knowledge_bases"):
            if isinstance(v, list):
                out[k] = [str(x) for x in v if x is not None]
            elif isinstance(v, str):
                # Sometimes the LLM emits a comma-separated string
                out[k] = [s.strip() for s in v.split(",") if s.strip()]
            # else: drop
            continue
        # Booleans
        if k in ("data_read", "data_write", "human_fallback", "trace_enabled"):
            if isinstance(v, bool):
                out[k] = v
            continue
        # Numbers
        if k in ("max_call_count", "max_retries", "max_iterations", "max_tokens"):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = int(v)
            continue
        if k in ("temperature", "top_p"):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out[k] = float(v)
            continue
        # Strings
        if isinstance(v, str):
            v = v.strip()
            if v:
                out[k] = v
        elif isinstance(v, (int, float, bool)):
            out[k] = v
    return out


def _build_decision_summary_fence(payload: dict) -> str:
    """Build the canonical ``:::decision-summary\\n{json}\\n:::`` block.

    The frontend's ``BuilderMessageBubble.extractDecisionSummary`` parses
    exactly this format.  Used by ``_persist_decision_summary_pause`` to
    append a synthetic fence for the intercept (R5) and force-pause (R6)
    paths where the LLM did not emit one itself.
    """
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return f":::decision-summary\n{body}\n:::"


def _persist_decision_summary_pause(
    db: Session,
    conv: AgentConversation,
    messages: list[dict],
    assistant_msg_id: str,
    tool_calls_for_frontend: list[dict],
    raw_assistant_text: str,
    tool_call_payload: dict | None = None,
) -> tuple[bool, str, str]:
    """If the assistant text contains a `:::decision-summary` block,
    parse it, persist the pending payload to ``conv.metadata_``,
    save the (block-stripped) assistant message, commit, and return
    ``(True, stripped_text, status_note)``.

    Otherwise return ``(False, raw_assistant_text, "")`` and do not
    touch the database.

    Centralising this here keeps v2 main, v2 resume, and v3 stream
    consistent — the only difference between those paths is the SSE
    wrapper.

    R5: when ``tool_call_payload`` is provided, the function uses it as
    the draft payload INSTEAD of parsing the text block. This is the
    intercept path — the LLM's `create_agent` tool-call arguments are
    the draft, so we don't need the LLM to emit the literal fence.
    The two pathways (fence parser + tool-call intercept) converge on
    the same persistence logic below.
    """
    if tool_call_payload is not None:
        # Intercept path: tool-call args are the draft, skip the fence parser.
        payload = tool_call_payload
    else:
        payload = parse_decision_summary_block(raw_assistant_text)
    if payload is None:
        return False, raw_assistant_text, ""

    clean_payload = _sanitize_decision_payload(payload)
    if not clean_payload.get("name"):
        # A decision summary without a name is not actionable. Refuse to
        # pause — let the LLM try again on the next iteration.
        logger.warning(
            "Decision summary found but no 'name' field; ignoring in conv %s",
            conv.id,
        )
        return False, raw_assistant_text, ""

    # The bubble's ``extractDecisionSummary`` parses the
    # ``:::decision-summary`` block from ``message.content`` and renders
    # DecisionSummaryCard inline.  To make the card render on initial
    # load and on page reload, the fence must be PERSISTED in the
    # message content.
    #
    # - Fence path: the LLM already emitted the fence; use raw text as-is.
    # - Intercept / force-pause path: the LLM did not emit a fence;
    #   append a synthetic one constructed from the sanitised payload so
    #   the bubble can still render the card from message.content alone.
    raw_for_persistence = raw_assistant_text
    if tool_call_payload is not None and parse_decision_summary_block(raw_assistant_text) is None:
        raw_for_persistence = (
            raw_assistant_text.rstrip()
            + "\n\n"
            + _build_decision_summary_fence(clean_payload)
        )
    # ``stripped`` is the prose-only text used for the streaming delta so
    # the user never sees the literal fence while text is streaming.
    stripped = strip_decision_summary_block(raw_for_persistence)
    # Persist the assistant message WITH the fence present.
    assistant_msg = {
        "id": assistant_msg_id,
        "role": "assistant",
        "content": raw_for_persistence,
        "created_date": datetime.now(timezone.utc).isoformat(),
    }
    if tool_calls_for_frontend:
        assistant_msg["tool_calls"] = tool_calls_for_frontend
    # Surface create_artifact results as artifacts
    _artifacts = _collect_artifact_results(
        tool_calls_for_frontend, assistant_msg_id, conv.id, db,
    )
    if _artifacts:
        assistant_msg["artifacts"] = _artifacts
    messages.append(assistant_msg)

    # Update conversation metadata so the frontend knows to render the
    # Decision Summary card instead of waiting for more text.
    # CRITICAL: create a NEW dict (shallow copy) so SQLAlchemy's JSON
    # column detects the change. Mutating the existing dict in-place and
    # re-assigning the same object reference is a no-op for change
    # tracking — the ``awaiting_decision_summary`` / ``pending_agent_payload``
    # keys would never be persisted (the checkpoint block above does this
    # correctly with ``dict(conv.metadata_ or {})``).
    md = dict(conv.metadata_ or {})
    md["awaiting_decision_summary"] = True
    md["pending_agent_payload"] = clean_payload
    conv.metadata_ = md
    # Re-assign messages to trigger SQLAlchemy JSON change detection.
    # The checkpoint code in the v3 stream loop may have rebound
    # conv.messages to a different list object, so mutating the closure
    # variable alone is not enough.
    conv.messages = list(messages)
    conv.updated_date = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception as e:
        logger.error("Failed to persist decision-summary pause: %s", e)
        db.rollback()
        return False, raw_assistant_text, ""

    logger.info(
        "Decision summary pause triggered for conv %s (payload keys=%s)",
        conv.id, sorted(clean_payload.keys()),
    )
    return True, stripped, "awaiting_decision_summary"


# ---------------------------------------------------------------------------
# create_agent tool-call intercept (R5)
# ---------------------------------------------------------------------------
# DeepSeek (and other LLMs that don't reliably emit fenced markdown) tend to
# PARAPHRASE the build action in prose instead of producing the literal
# `:::decision-summary` block. The fence-based parser then finds nothing and
# the agent is created directly with no review step. R5's fix: instead of
# relying on the LLM to wrap its draft, we treat the LLM's `create_agent`
# tool-call `arguments` JSON as the draft ITSELF. We intercept the call
# before it executes, sanitise the args through the same allow-list used by
# the fence path, and hand off to `_persist_decision_summary_pause` with
# `tool_call_payload=...`. The two paths (fence + intercept) converge on the
# same persistence helper, so the frontend behaviour is identical.

def _intercept_create_agent(
    parsed_calls: list[dict],
) -> tuple[bool, dict | None, int]:
    """Scan ``parsed_calls`` for any ``create_agent`` invocation.

    Returns ``(True, sanitized_args, call_index)`` on the first hit, where
    ``sanitized_args`` is already filtered through ``_sanitize_decision_payload``
    so the caller doesn't have to. ``call_index`` is the position in
    ``parsed_calls`` (so the caller can drop that entry and keep the rest).

    Returns ``(False, None, -1)`` when no ``create_agent`` call is present.
    """
    for i, call in enumerate(parsed_calls):
        if call.get("tool_name") == "create_agent":
            clean = _sanitize_decision_payload(call.get("args") or {})
            return True, clean, i
    return False, None, -1


# ---------------------------------------------------------------------------
# Force-pause (R6): build a decision summary from the user message when
# the LLM has explored for too long without calling `create_agent`.
# ---------------------------------------------------------------------------
# Root cause for the force-pause: DeepSeek tends to NARRATE a decision
# summary in prose ("Presenting the decision summary for your review:")
# and then keep calling `list_tools` / `skills` / `list_market_agents` in
# a discovery loop, NEVER actually emitting the `:::decision-summary`
# fence and NEVER calling the `create_agent` tool. The user sees only
# "Searching available capabilities..." spinners and concludes the agent
# is stuck.
#
# R5 added the `create_agent` tool-call intercept, but that only fires
# when the LLM actually calls `create_agent`. In the save-directly
# scenario the LLM never reaches that call — it stays in discovery
# mode despite the system prompt's "skip GATHER" rule.
#
# Force-pause breaks the loop deterministically: after 2 tool iterations
# (the LLM has had its chance to discover), if the user message contains
# a "save directly" / "build it" cue OR a complete spec (Name:/Project:/
# Description: lines), we auto-build a decision summary from the user
# message + sensible defaults and call the same
# `_persist_decision_summary_pause` helper. The user gets the
# Decision Summary card and can finish the build with one click.

# Compiled once at module load — these patterns are used on every save-
# directly build.
_FORCE_PAUSE_SPEC_PATTERNS = {
    "name": re.compile(
        r"^\s*[-\*]?\s*name\s*[:：]\s*(.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "project": re.compile(
        r"^\s*[-\*]?\s*project\s*[:：]\s*(.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
    "description": re.compile(
        r"^\s*[-\*]?\s*description\s*[:：]\s*(.+?)\s*$",
        re.IGNORECASE | re.MULTILINE,
    ),
}

# Phrases that indicate the user wants us to build immediately, not ask
# clarifying questions. Matched case-insensitively as substrings.
_FORCE_PAUSE_INTENT_PHRASES = (
    "save directly",
    "save it directly",
    "build it now",
    "build it directly",
    "create it now",
    "create it directly",
    "build directly",
    "create directly",
    "save as an agentapp",
    "save as agentapp",
)


def _user_wants_save_directly(user_content: str) -> bool:
    """True when the user message contains a save-directly / build-it
    intent phrase (case-insensitive substring match)."""
    if not user_content:
        return False
    lower = user_content.lower()
    return any(phrase in lower for phrase in _FORCE_PAUSE_INTENT_PHRASES)


def _build_forced_decision_summary(user_content: str) -> dict:
    """Build a decision-summary payload from the user message + sensible
    defaults. Returns a dict that has already been sanitised through
    ``_sanitize_decision_payload`` (which is a no-op on the keys we
    produce, but defensive — keeps the contract uniform with the other
    code paths).

    Returns an empty dict if no `name` can be extracted — the caller
    treats that as "can't force-pause" and falls through.
    """
    out: dict = {}
    for key, pattern in _FORCE_PAUSE_SPEC_PATTERNS.items():
        m = pattern.search(user_content or "")
        if m:
            out[key] = m.group(1).strip()
    # Sensible defaults for the rest — these are the same defaults the
    # system prompt suggests, applied automatically when the user hasn't
    # specified them.
    out.setdefault("model", "automatic")
    out.setdefault("agent_type", "sequential")
    out.setdefault("capabilities", [])
    out.setdefault("skills", [])
    out.setdefault("knowledge_bases", [])
    out.setdefault("data_read", True)
    out.setdefault("data_write", False)
    out.setdefault("human_fallback", True)
    out.setdefault("trace_enabled", True)
    out.setdefault("log_level", "info")
    out.setdefault("status", "draft")
    return out


def _internal_tool_projection(tool_name: str) -> dict:
    """Build a display_projection block that tells the frontend to render this
    internal tool call as a one-line progress indicator instead of a full
    collapsible panel with arguments and results.
    """
    label = INTERNAL_TOOL_LABELS.get(tool_name, "Searching available capabilities...")
    return {
        "hide_details": True,
        "details_redacted": True,
        "label": label,
        "active_label": label,
        "is_internal": True,
    }


# Per-agent tool-loop guardrail config. 2026-07-28: Skill Agent gets tighter
# no-progress thresholds so a repeated identical `search_skills` call (e.g.
# the model looping on "got 0 results, search again with the same query")
# gets warned on the 2nd call and blocked on the 3rd, instead of running
# the full default budget (warn=2, block=5). All other agents use the
# default config from tool_loop_guardrails.ToolGuardrailConfig().
#
# Threshold semantics (from tool_loop_guardrails.ToolGuardrailConfig +
# ToolLoopGuardController): the controller increments a counter on every
# AFTER_CALL where (args, result) are identical to the previous call,
# then WARS at repeat_count >= no_progress_warn_after and BLOCKS on
# the next BEFORE_CALL when the stored record[1] >= no_progress_block_after.
# So:
#   - no_progress_warn_after=2  →  2nd identical call is warned
#   - no_progress_block_after=2 →  3rd identical call is blocked
#: Tool calls allowed to pair with a :::options clarification block — the
#: skill/agent-writing tools whose methodology text may legitimately mention
#: the block syntax inside their skill_md / definition.
_OPTIONS_SAFE_TOOLS = frozenset({
    "create_skill", "update_skill", "delete_skill",
    "create_agent", "update_agent",
    "create_automation", "update_automation",
})


def _options_clarification(content: str, tool_names: list[str]) -> bool:
    """True when the assistant message asks a :::options clarifying question
    and the turn must end here.

    An options block is a question to the user — running research tools while
    waiting for an answer is the mid-clarify bug (weak LLMs ignore the prose
    rule). Returns True (→ suppress tools + break) when the content contains
    an options block AND any tool call is NOT a skill/agent-writing tool
    (those may legitimately reference the block syntax in their output).
    Also returns True with no tool calls: a clarification block always ends
    the turn (skip the verification / goal-contract gates, which would
    misread a question as an unmet deliverable).
    """
    if ":::options" not in content:
        return False
    if not tool_names:
        return True
    return not all(n in _OPTIONS_SAFE_TOOLS for n in tool_names)


def _loop_guard_config_for(agent_app) -> ToolGuardrailConfig:
    if agent_app is not None and getattr(agent_app, "name", None) == "skill_agent":
        return ToolGuardrailConfig(
            no_progress_warn_after=2,
            no_progress_block_after=2,
        )
    return ToolGuardrailConfig()


# Map LLM tool names to display names for frontend tool_calls.
# The frontend's extractAgentId() searches for tool names containing "agentapp".
TOOL_DISPLAY_NAMES = {
    "create_agent": "AgentApp.create",
    "update_agent": "AgentApp.update",
    "create_skill": "Tool.create",
    "update_skill": "Tool.update",
    "search_skills": "search_skills",
    "list_tools": "list_tools",
    "list_market_agents": "list_market_agents",
    "create_automation": "AutomationTask.create",
    "update_automation": "AutomationTask.update",
    "list_knowledge_bases": "list_knowledge_bases",
    # Core capability tools
    "web_search": "web_search",
    "web_extract": "web_extract",
    "memory": "memory",
    "todo": "todo",
    "read_file": "read_file",
    "write_file": "write_file",
    "image_generation": "image_generation",
    "execute_code": "execute_code",
    "delegate_task": "delegate_task",
    "ask_data_agent": "ask_data_agent",
    "run_sandbox_skill": "run_sandbox_skill",
    # Phase 1: admin
    "update_env_config": "update_env_config",
    "docker_compose_restart": "docker_compose_restart",
    # Phase 2: quick wins
    "url_safety": "url_safety",
    "fuzzy_match": "fuzzy_match",
    "path_security": "path_security",
    "patch_parser": "patch_parser",
    "process_registry_list": "process_registry.list",
    "process_registry_tail": "process_registry.tail",
    "process_registry_kill": "process_registry.kill",
    "env_passthrough": "env_passthrough",
    "credential_files": "credential_files",
    "kanban": "kanban",
    "cronjob": "cronjob",
    "clarify": "clarify",
    "slash_confirm": "slash_confirm",
    "checkpoint_manager": "checkpoint_manager",
    "session_search": "session_search",
    "interrupt": "interrupt",
    "osv_check": "osv_check",
    "tirith_security": "tirith_security",
    "approval": "approval",
    "mixture_of_agents": "mixture_of_agents",
    # Phase 3: skills
    "skills": "skills",
    "skills_hub": "skills.hub",
    "skills_sync": "skills.sync",
    "skills_guard": "skills.guard",
    "skill_manager": "skill_manager",
    "skill_provenance": "skill.provenance",
    "skill_usage": "skill.usage",
    # Phase 4: LLM
    "openrouter": "openrouter",
    "xai_http": "xai_http",
    "x_search": "x_search",
    "yuanbao": "yuanbao",
    # Phase 5: media
    "tts": "tts",
    "video_generation": "video_generation",
    "transcription": "transcription",
    "vision": "vision",
    "voice_mode": "voice_mode",
    # Phase 6: browser (Playwright replaced by the agent-browser CLI wrapper)
    "agent_browser": "agent_browser",
    "computer_use": "computer_use",
    # Phase 7: communication
    "discord": "discord",
    "feishu_doc": "feishu_doc",
    "feishu_drive": "feishu_drive",
    "send_message": "send_message",
    "homeassistant": "homeassistant",
    "microsoft_graph": "microsoft_graph",
    "microsoft_graph_auth": "microsoft_graph_auth",
    # Phase 8: MCP
    "mcp": "mcp",
    "mcp_oauth": "mcp.oauth",
    "mcp_oauth_manager": "mcp.oauth_manager",
}


# ---------------------------------------------------------------------------
# Anti-hallucination guardrails for agents with bound data sources
# ---------------------------------------------------------------------------

# Max retries when the LLM fabricates data instead of calling ask_data_agent.
# Total LLM calls for a data question = 1 (initial) + 2 (retries) = 3.
MAX_GUARDRAIL_RETRIES = 2

# Keywords that indicate the user is asking about data. Intentionally broad —
# false positives just cause a harmless retry. Case-insensitive substring match.
_DATA_QUESTION_KEYWORDS = [
    "customer", "revenue", "sales", "order", "top", "list", "count", "sum",
    "average", "total", "report", "dashboard", "data", "query", "database",
    "table", "how many", "statistics", "stat", "trend", "monthly", "weekly",
    "daily", "profit", "expense", "product", "employee", "user", "record",
    "transaction", "invoice", "payment", "metric", "kpi", "growth", "rate",
    "ratio", "percentage", "breakdown", "summary", "aggregat", "group by",
    "filter", "segment", "category", "region", "country", "city", "amount",
    "balance", "budget", "forecast", "pipeline", "conversion", "retention",
    "churn", "acquisition", "margin", "cost", "price", "quantity", "inventory",
    "stock", "shipment", "deliver", "ticket", "issue", "request",
]

# Phrases that indicate the user is NOT asking a data question (e.g., creating
# an agent, updating a skill). These override the data-question keywords to
# avoid false positives when a message like "create a customer support agent"
# matches "customer" but is actually a create operation.
_NON_DATA_QUESTION_PHRASES = [
    "create an agent", "create a new agent", "create agent",
    "update agent", "update the agent",
    "create a skill", "create skill", "create a new skill",
    "update skill", "update the skill",
    "create automation", "create an automation",
    "update automation", "update the automation",
    "create a task", "create task",
    "help me create", "help me build", "help me set up",
    "what can you do", "who are you",
]


_TRACE_FENCE_RE = re.compile(
    r":::trace\s*\n(.*?)\n\s*:::",
    re.DOTALL,
)


def _derive_trace_from_response(
    assistant_content: str,
    tool_calls_for_frontend: list[dict] | None = None,
    started_at: datetime | None = None,
) -> list[dict]:
    """Derive a `trace` list (Reasoning & actions) for the assistant message.

    Priority:
      1. Parse a `:::trace\\n[...]\\n:::` fence from the LLM's text response.
      2. Fall back: build one entry per tool call in ``tool_calls_for_frontend``.
      3. If neither is available, return an empty list.

    Each trace step dict has:
      ``step``, ``type``, ``title``, ``detail``, ``status``, ``duration_ms``
    — matching the shape consumed by the ``ReasoningSummary`` frontend component.

    This is always-on (not gated on ``trace_enabled``); every Harness Agent
    reports its reasoning trace to the user. See AGT-0 / AGT-13.
    """
    # 1. Try the LLM-emitted fence first.
    if assistant_content and ":::trace" in assistant_content:
        m = _TRACE_FENCE_RE.search(assistant_content)
        if m:
            raw = m.group(1).strip()
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        # Validate and normalise each step
                        norm = []
                        for i, step in enumerate(parsed):
                            if not isinstance(step, dict):
                                continue
                            norm.append({
                                "step": step.get("step", i + 1),
                                "type": step.get("type", "tool_call"),
                                "title": step.get("title", step.get("name", f"Step {i + 1}")),
                                "detail": step.get("detail", step.get("description", "")),
                                "status": step.get("status", "completed"),
                                "duration_ms": step.get("duration_ms", 0),
                            })
                        if norm:
                            return norm
                except (json.JSONDecodeError, TypeError):
                    logger.debug("trace fence found but parse failed; falling back to tool_calls")

    # 2. Fall back: derive one trace step per executed tool call.
    tc_list = tool_calls_for_frontend or []
    if tc_list:
        now = datetime.now(timezone.utc)
        started = started_at or now
        elapsed = max(0, int((now - started).total_seconds() * 1000)) if started else 0
        # Distribute elapsed across steps evenly (rough approximation)
        per_step_ms = max(1, elapsed // len(tc_list)) if elapsed else 0

        trace = []
        for i, tc in enumerate(tc_list):
            name = tc.get("name", "")
            status = tc.get("status", "completed")
            display_proj = tc.get("display_projection") or {}
            # For hide_details / internal tools, use the projected label
            if display_proj.get("hide_details"):
                title = display_proj.get("label", name)
                detail = display_proj.get("done_label", "")
            else:
                title = name
                # Grab a short result excerpt
                results = tc.get("results")
                if isinstance(results, dict):
                    detail = results.get("error", results.get("summary", "")) or (
                        "success" if results.get("success") else "failed"
                    )
                else:
                    detail = "executed"
            trace.append({
                "step": i + 1,
                "type": "tool_call",
                "title": title,
                "detail": detail,
                "status": "completed" if status in ("completed", "success") else "failed",
                "duration_ms": per_step_ms,
            })
        return trace

    # 3. No traceable content — empty list (frontend hides the card).
    return []


def _collect_artifact_results(
    tool_calls_for_frontend: list[dict],
    message_id: str,
    conversation_id: str,
    db: Session | None = None,
) -> list[dict]:
    """Extract artifact metadata from artifact-producing tool results.

    For each tool_call named 'create_artifact' or 'run_sandbox_skill'
    (both engines emit the same canonical result shape) where
    result.success is True, extract the {artifact_id, file_url,
    preview_url, title, type, file_name, mime_type, file_size,
    has_preview} payload and link the artifact to the given message via
    ArtifactService.link_to_message().

    Returns a list of artifact dicts suitable for the frontend's
    ``message.artifacts`` field.

    Idempotent: ``link_to_message`` de-duplicates ``message_artifacts``
    rows per (artifact_id, message_id), and the returned list is
    de-duplicated by ``artifact_id`` (first occurrence wins) so callers
    may safely re-run collection over a tool_call list that contains
    both previously collected and new records.
    """
    artifacts: list[dict] = []
    _seen_ids: set = set()
    for tc in (tool_calls_for_frontend or []):
        name = tc.get("name")
        result = tc.get("results") or {}
        # Live dashboard tool — surface its lightweight artifact reference
        # (no file/Artifact DB row; the frontend renders a live card + popup
        # that polls /api/dashboards/{id}/query).
        if name == "create_dashboard":
            if (isinstance(result, dict) and result.get("success")
                    and isinstance(result.get("artifact"), dict)):
                art = result["artifact"]
                if art.get("dashboard_id") and art["dashboard_id"] not in _seen_ids:
                    _seen_ids.add(art["dashboard_id"])
                    artifacts.append(art)
            continue
        # Both artifact engines emit the same canonical result shape:
        # create_artifact (direct/exporter path) and run_sandbox_skill
        # (Docker sandbox path). Collect them identically.
        if name not in ("create_artifact", "run_sandbox_skill"):
            continue
        if isinstance(result, dict) and result.get("success") and result.get("artifact_id"):
            if result["artifact_id"] in _seen_ids:
                continue
            _seen_ids.add(result["artifact_id"])
            artifact_entry = {
                "artifact_id": result.get("artifact_id"),
                "version_id": result.get("version_id"),
                "version_number": result.get("version_number"),
                "file_url": result.get("file_url"),
                "preview_url": result.get("preview_url"),
                "title": result.get("title", ""),
                "type": result.get("type", ""),
                "file_name": result.get("file_name", ""),
                "mime_type": result.get("mime_type", ""),
                "file_size": result.get("file_size"),
                "has_preview": result.get("has_preview", False),
            }
            # Forward the sidecar preview_artifact_id when the tool
            # result carries one.  The dedup step below may overwrite
            # it for HTML siblings consumed as a sidecar.
            if result.get("preview_artifact_id"):
                artifact_entry["preview_artifact_id"] = result["preview_artifact_id"]
            artifacts.append(artifact_entry)

            # Link artifact to message so get_message_artifacts works
            if db is not None and message_id and conversation_id:
                try:
                    from app.services.artifacts.artifact_service import ArtifactService
                    svc = ArtifactService(db)
                    svc.link_to_message(
                        artifact_id=result["artifact_id"],
                        message_id=message_id,
                        conversation_id=conversation_id,
                        display_order=len(artifacts),
                    )
                except Exception as link_err:
                    logger.warning(
                        "Failed to link artifact %s to message %s: %s",
                        result.get("artifact_id"), message_id, link_err,
                    )

    # ----------------------------------------------------------------
    # Layer 1 (de-dup): when the same turn produces BOTH a file-format
    # artifact (docx / pptx / xlsx / pdf / md) AND an HTML artifact
    # whose title is the same report (the rich-HTML render the user
    # sees when they ask "make a docx report"), drop the HTML from the
    # chat payload and lift its preview_url / preview_artifact_id onto
    # the file-format artifact.  This is the "one card per file
    # format" rule the user asked for: the chat shows the DOCX card,
    # but its preview pane renders the interactive HTML dashboard.
    # ----------------------------------------------------------------
    _FILE_FORMATS = {"docx", "pptx", "xlsx", "pdf", "md"}
    _HTML_VARIANTS = {"html", "html_report"}
    # File-name extensions to strip when comparing titles — handles the
    # case where one tool returned the user-facing title ("Address
    # Distribution Report by Region") and the other returned the
    # auto-generated file name ("Address_Distribution_Report.docx").
    _EXT_PATTERN = re.compile(
        r"\.(docx|pptx|xlsx|pdf|md|html|htm|json|csv|tsv|txt)$",
        re.IGNORECASE,
    )

    def _title_key(t: str) -> str:
        """Normalize a title for fuzzy matching.

        Strips a trailing " (preview)" marker, removes a file extension
        (.docx, .html, …), converts underscores to spaces, collapses
        whitespace, and applies NFKD unicode normalization so
        "Café Report" matches "Cafe Report" (stripped accent).
        """
        s = (t or "").strip().rstrip(" (preview)").strip()
        s = _EXT_PATTERN.sub("", s).strip()
        s = s.replace("_", " ")
        s = unicodedata.normalize("NFKD", s)
        # Drop combining marks (accents) but keep base letters.
        s = "".join(c for c in s if not unicodedata.combining(c))
        s = re.sub(r"\s+", " ", s)
        return s.lower().strip()

    def _titles_match(fm_t: str, h_t: str) -> bool:
        """Fuzzy title match for the one-card-per-file-format rule.

        Two titles match when:
        - They're equal after normalization, OR
        - One is a strict prefix of the other (so "sales report" matches
          "sales report by region" and "sales report.docx"), OR
        - They share the first 2+ significant words (so "address
          distribution report.docx" matches "address distribution report
          by region" even when no full prefix relation holds).
        """
        a, b = _title_key(fm_t), _title_key(h_t)
        if not a or not b:
            return False
        if a == b or a.startswith(b) or b.startswith(a):
            return True
        # Token-prefix fallback.  Stop at the first mismatch; require
        # at least 2 common tokens so accidental overlaps (e.g. "q1
        # revenue" and "q1 report") don't dedup.
        a_tokens = a.split(" ")
        b_tokens = b.split(" ")
        common = 0
        for x, y in zip(a_tokens, b_tokens):
            if x == y:
                common += 1
            else:
                break
        return common >= 2

    file_fmts = [a for a in artifacts if (a.get("type") or "").lower() in _FILE_FORMATS]
    # Split the HTML siblings into two buckets so we can prefer the
    # RICH ``html_report`` (the full interactive dashboard produced by
    # ``finalize_into_artifact``) over the often-sparse ``html``
    # sidecar the sandbox creates for the file-format artifact.
    rich_html_arts = [a for a in artifacts if (a.get("type") or "").lower() == "html_report"]
    plain_html_arts = [a for a in artifacts if (a.get("type") or "").lower() in {"html", "html_chart"}]

    if file_fmts:
        consumed: set = set()
        for fm in file_fmts:
            fm_title = fm.get("title", "")
            explicit_sidecar_id = fm.get("preview_artifact_id")

            # ------------------------------------------------------------------
            # Pick a single sidecar for ``fm`` in priority order:
            #   1. Rich ``html_report`` whose title matches  (Claude-style
            #      dashboard from ``finalize_into_artifact`` — the user's
            #      favourite preview).
            #   2. The explicit ``preview_artifact_id`` set by the tool
            #      handler (sandbox or create_artifact).
            #   3. Any plain ``html`` / ``html_chart`` whose title matches
            #      (sparse sandbox fallback).
            # ------------------------------------------------------------------
            chosen: dict | None = None

            # 1. rich html_report by title
            for h in rich_html_arts:
                if h["artifact_id"] in consumed:
                    continue
                if not _titles_match(fm_title, h.get("title", "")):
                    continue
                chosen = h
                break

            # 2. explicit sidecar (only if it is still in this turn's
            #    artifact list, i.e. the same tool handler also emitted
            #    the sidecar)
            if chosen is None and explicit_sidecar_id:
                for h in rich_html_arts + plain_html_arts:
                    if h["artifact_id"] == explicit_sidecar_id and h["artifact_id"] not in consumed:
                        chosen = h
                        break

            # 3. plain html by title
            if chosen is None:
                for h in plain_html_arts:
                    if h["artifact_id"] in consumed:
                        continue
                    if not _titles_match(fm_title, h.get("title", "")):
                        continue
                    chosen = h
                    break

            if chosen is not None:
                # Lift the rich HTML's preview onto the file-format
                # artifact so ArtifactPreviewPane iframes the rich HTML
                # instead of falling back to mammoth for the docx body.
                fm["preview_url"] = chosen.get("preview_url") or fm.get("preview_url")
                fm["preview_artifact_id"] = chosen["artifact_id"]
                # If the sidecar we picked was NOT the explicit one,
                # drop the explicit sidecar from the response too so the
                # chat shows exactly one card per file format.
                if explicit_sidecar_id and explicit_sidecar_id != chosen["artifact_id"]:
                    consumed.add(explicit_sidecar_id)
                consumed.add(chosen["artifact_id"])

        if consumed:
            artifacts = [a for a in artifacts if a["artifact_id"] not in consumed]
            logger.info(
                "Layer 1 dedup: dropped %d HTML sibling(s) in favor of file-format cards: %s",
                len(consumed),
                ", ".join(
                    f"{a.get('type')}={a.get('artifact_id')}"
                    for a in file_fmts
                ),
            )

    # ── Layer 2 dedup — collapse file-format sibling duplicates ──
    # When finalize.py creates two file-format artifacts for the same
    # document (e.g. eager_render_default + run_sandbox_skill both
    # produce a docx), keep only one.  Group by normalized title (or
    # type + file_name as fallback) and keep the highest version_number.
    file_artifacts = [
        a for a in artifacts
        if (a.get("type") or "").lower() in _FILE_FORMATS
    ]
    if len(file_artifacts) > 1:
        # Group by (normalized_title, type) or (file_name, type) as fallback
        from collections import defaultdict
        groups: dict[tuple, list[dict]] = defaultdict(list)
        for a in file_artifacts:
            key = (
                (a.get("title") or "").strip().lower(),
                a.get("type") or "",
            )
            if not key[0]:  # fallback to file_name
                key = (
                    (a.get("file_name") or "").strip().lower(),
                    a.get("type") or "",
                )
            groups[key].append(a)

        consumed2: set[str] = set()
        for key, group in groups.items():
            if len(group) < 2:
                continue
            # Keep the one with the highest version_number; if tied,
            # keep the one with preview_url (sandbox) over the one without
            sorted_group = sorted(
                group,
                key=lambda a: (
                    a.get("version_number") or 0,
                    1 if a.get("preview_url") else 0,
                ),
                reverse=True,
            )
            for dup in sorted_group[1:]:
                consumed2.add(dup["artifact_id"])
                logger.info(
                    "Layer 2 dedup: dropping file-format sibling %s (type=%s, v=%s) "
                    "in favor of %s (type=%s, v=%s)",
                    dup.get("artifact_id"), dup.get("type"), dup.get("version_number"),
                    sorted_group[0].get("artifact_id"), sorted_group[0].get("type"),
                    sorted_group[0].get("version_number"),
                )

        if consumed2:
            artifacts = [a for a in artifacts if a["artifact_id"] not in consumed2]

    return artifacts


def _should_finalize_no_data(result: object, doc_format: object) -> bool:
    """True only when ask_data_agent RAN a query that returned 0 rows.

    ``rows == []`` means "query executed, empty result set" — finalize a
    graceful no-data artifact. ``rows is None`` means the data agent
    answered conversationally without ever querying (clarification,
    refusal, …) — a "0 rows" narrative would be misleading, so we must
    NOT take the no-data branch in that case.
    """
    return (
        isinstance(result, dict)
        and bool(result.get("success"))
        and result.get("rows") == []
        and bool(doc_format)
    )


def _checkpoint_partial_assistant_msg(
    db: Session,
    conv,
    messages: list[dict],
    assistant_msg_id: str,
    tool_calls_for_frontend: list[dict],
    artifact_ids: list[str],
) -> None:
    """Persist the in-flight assistant message mid-turn.

    A dropped SSE stream or a crash after this point must not erase
    finished tool work (report cards, artifact ids). The partial record
    is written as ``conv.messages + [partial]`` WITHOUT mutating the
    in-flight ``messages`` list, so the final message assembly still
    appends the authoritative ``assistant_msg`` (same id) and cleanly
    overwrites this checkpoint. Best-effort: never breaks the turn.

    NOTE: ``db.commit()`` commits the WHOLE session, not just ``conv`` —
    any other dirty ORM state pending at checkpoint time is committed
    along with it (and ORM attributes expire per ``expire_on_commit``).
    Tool handlers must not rely on uncommitted state surviving past this
    point.
    """
    try:
        partial = {
            "id": assistant_msg_id,
            "role": "assistant",
            "content": "",
            "created_date": datetime.now(timezone.utc).isoformat(),
        }
        if tool_calls_for_frontend:
            partial["tool_calls"] = tool_calls_for_frontend
        if artifact_ids:
            partial["artifact_ids"] = list(artifact_ids)
        conv.messages = list(messages) + [partial]
        conv.updated_date = datetime.now(timezone.utc)
        db.commit()
    except Exception as ckpt_err:
        logger.warning("mid-turn checkpoint failed (non-fatal): %s", ckpt_err)


def _is_data_question(text: str | None) -> bool:
    """Heuristic: does this user message look like a data question?

    Two-step check:
    1. First, check for non-data phrases (create/update/delete operations,
       capability questions). If any match, return False — these are NOT
       data questions even if they contain data keywords like "customer".
    2. Then check for data-question keywords. Case-insensitive substring match.

    Args:
        text: The user's message text (may be None).

    Returns:
        True if the message looks like a data question.
    """
    if not text:
        return False
    lower = text.lower()

    # Step 1: exclude non-data operations (create/update agents, skills, etc.)
    for phrase in _NON_DATA_QUESTION_PHRASES:
        if phrase in lower:
            return False

    # Step 2: check for data-question keywords
    return any(kw in lower for kw in _DATA_QUESTION_KEYWORDS)


# Return type for _check_hallucination_guardrail — structured result instead
# of overloading string content for control flow.
#   action="none"     → guardrail did not trigger, accept the response
#   action="nudge"    → inject the message and retry the LLM call
#   action="fallback" → replace the hallucinated content with the message and break
from typing import Any, NamedTuple


class _GuardrailResult(NamedTuple):
    action: str  # "none" | "nudge" | "fallback"
    message: str


def _check_hallucination_guardrail(
    user_message: str,
    data_ctx_extras: dict,
    tool_calls_made: list[dict],
    iteration: int,
    guardrail_retries: int,
) -> _GuardrailResult:
    """Detect hallucination: LLM emitted text with no tool call when the
    agent has bound data sources and the question is data-related.

    Args:
        user_message: The original user message.
        data_ctx_extras: Context dict from prepare_data_source_runtime.
                         Must contain 'bound_kb_ids' for the guardrail to trigger.
        tool_calls_made: List of tool call records made so far (each has 'name').
        iteration: Current loop iteration (0-based).
        guardrail_retries: How many guardrail retries have already happened.

    Returns:
        _GuardrailResult with:
        - action="none": guardrail did not trigger (safe to accept the response).
        - action="nudge": inject message as a system nudge and retry the LLM call.
        - action="fallback": replace hallucinated content with message and break.
    """
    # Only trigger when the agent has bound data sources
    bound_kb_ids = (data_ctx_extras or {}).get("bound_kb_ids") or []
    if not bound_kb_ids:
        return _GuardrailResult("none", "")

    # Only trigger for data-related questions
    if not _is_data_question(user_message):
        return _GuardrailResult("none", "")

    # If ask_data_agent was already called, the agent did the right thing.
    # Check both the raw tool name and the display name (which happens to be
    # the same for ask_data_agent, but this is more robust).
    called_names = {tc.get("name", "") for tc in tool_calls_made}
    if "ask_data_agent" in called_names:
        return _GuardrailResult("none", "")

    # Retries exhausted — return safe fallback (no fabricated data)
    if guardrail_retries >= MAX_GUARDRAIL_RETRIES:
        logger.warning(
            "Hallucination guardrail: retries exhausted for data question "
            "(bound_kb_ids=%s). Returning safe fallback.",
            bound_kb_ids,
        )
        return _GuardrailResult("fallback", (
            "I need to query the database to answer this question, but I was "
            "unable to do so after multiple attempts. Please try rephrasing "
            "your question, or check that the bound data sources are accessible."
        ))

    # Trigger a retry with a hard nudge
    logger.warning(
        "Hallucination guardrail: LLM emitted no tool call for data question "
        "(bound_kb_ids=%s, retry %d/%d). Injecting nudge.",
        bound_kb_ids, guardrail_retries + 1, MAX_GUARDRAIL_RETRIES,
    )
    return _GuardrailResult("nudge", (
        "SYSTEM GUARDRAIL: You have bound database data sources but did NOT call "
        "the `ask_data_agent` tool. Your previous response appears to contain "
        "fabricated data. You MUST call `ask_data_agent` with a clear question "
        "to fetch real data from the database. Do NOT fabricate data, invent "
        "customer names, or generate data tables without calling `ask_data_agent`. "
        "Any data in your response must come from the tool result. Call "
        "`ask_data_agent` now."
    ))


class _OrchGuardSkipped(Exception):
    """Internal control-flow sentinel (Fix C).

    Raised inside the finalize-phase orchestrator ``try`` block when the
    dashboard-orchestrator guard fired, so the ENTIRE post-loop orchestrator
    (marker fulfillment + ``ensure_artifact_for_doc_request``) is skipped on a
    dashboard-intent turn where the build tool was never called. Caught by a
    dedicated ``except _OrchGuardSkipped`` clause (no log noise)."""


class _AnswerGateResult(NamedTuple):
    action: str  # "none" | "nudge" | "disclose"
    message: str


def _gate_tool_results_from_frontend(tool_calls_for_frontend: list[dict]) -> list[dict]:
    """Extract summarized tool payloads from frontend tool-call records.

    Each record carries the tool result under ``results``; we forward the raw
    payload (with the tool name attached) so ``summarize_tool_results`` inside
    ``evaluate_answer`` can normalize it. Never dumps full payloads anywhere.
    """
    out: list[dict] = []
    for tc in tool_calls_for_frontend or []:
        if not isinstance(tc, dict):
            continue
        name = tc.get("name") or tc.get("tool_name") or ""
        results = tc.get("results")
        if isinstance(results, dict):
            raw = dict(results)
            raw.setdefault("tool", name)
            out.append(raw)
        elif isinstance(results, list) and results:
            out.append({"tool": name, "rows": results})
        elif isinstance(results, str) and results.strip():
            # Text payloads (web search / RAG summaries) feed the dimension
            # coverage detector; never dump full content.
            out.append({"tool": name, "text": results[:1200]})
        elif name:
            out.append({"tool": name})
    return out


async def _check_answer_verification_gate(
    user_message: str,
    tool_calls_for_frontend: list[dict],
    assistant_content: str,
    *,
    attempts: int,
    budget_remaining: int,
    catalog_meta: dict | None = None,
) -> _AnswerGateResult:
    """Universal Self-Evaluation & Re-Planning gate.

    Invoked at the synthesis boundaries (v2 / resume / v3) AFTER the
    hallucination guardrail, only when tools were actually called. Runs the
    hybrid evaluator off-thread with a timeout; ``nudge`` means the caller
    should append the message and continue the loop, ``disclose`` means the
    caller should append the gap disclosure to the final answer and break.
    Flag-off returns ``none`` immediately (byte-identical behavior).
    """
    if not getattr(settings, "SELF_EVAL_REPLAN_ENABLED", False):
        return _AnswerGateResult("none", "")
    if not tool_calls_for_frontend:
        return _AnswerGateResult("none", "")
    # CAD turns (Fusion 360 tools) skip the DATA-oriented self-eval gate.
    # The CAD loop has its own geometry verification (fusion360_verify_build);
    # the data evaluator does not understand CAD payloads and produces false
    # gaps — e.g. "requested dimensions not found in the data: taller, verify"
    # appended to a build that verified PASS. Verify-before-build tools are
    # the source of truth for these turns.
    for _tc in tool_calls_for_frontend:
        _tn = _tc.get("name") or _tc.get("tool_name") or ""
        if _tn.startswith("fusion360_"):
            return _AnswerGateResult("none", "")
    tool_results = _gate_tool_results_from_frontend(tool_calls_for_frontend)
    if not tool_results:
        return _AnswerGateResult("none", "")
    # Change 1 (2026-08-26): gate fast-path on the FIRST check. When the
    # turn already produced substantive data rows, skip the self-eval LLM
    # call entirely — rich answers are not second-guessed. This stops the
    # gate's nudge → re-synthesis → content_replace chain from mutating
    # content the user has already seen (the visual-collapse bug). The gate
    # still runs when attempts > 0 (genuine re-plan) and when NO data rows
    # were produced (the wrong/empty-table failure mode it exists to catch).
    if attempts == 0:
        from app.services.goal_contract import is_metadata_only_rows  # lazy
        for _gr in tool_results:
            _grows = _gr.get("rows")
            if isinstance(_grows, list) and _grows and not is_metadata_only_rows(_grows):
                logger.info(
                    "answer-verification gate: fast-path pass "
                    "(attempts=0, %d rows present)",
                    len(_grows),
                )
                return _AnswerGateResult("none", "")
    timeout_s = getattr(settings, "SELF_EVAL_LLM_GATE_TIMEOUT_S", 15.0)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                evaluate_answer,
                user_message,
                tool_results,
                assistant_content,
                attempts=attempts,
                budget_remaining=budget_remaining,
                catalog_meta=catalog_meta,
            ),
            timeout=timeout_s,
        )
    except Exception as exc:
        # Never block the stream on the gate: log and proceed.
        logger.warning(
            "answer-verification gate failed/timed out (non-fatal): %s", exc
        )
        return _AnswerGateResult("none", "")
    if result.status == "COMPLETE":
        logger.debug(
            "answer-verification gate: action=none status=COMPLETE attempts=%s "
            "budget=%s signals=%s",
            attempts, budget_remaining, result.signals,
        )
        return _AnswerGateResult("none", "")
    if result.status == "INCOMPLETE":
        logger.info(
            "answer-verification gate: action=nudge attempts=%s budget=%s "
            "signals=%s gaps=%s",
            attempts, budget_remaining, result.signals, result.gaps,
        )
        return _AnswerGateResult("nudge", build_replan_nudge(result))
    logger.info(
        "answer-verification gate: action=disclose attempts=%s budget=%s "
        "signals=%s gaps=%s",
        attempts, budget_remaining, result.signals, result.gaps,
    )
    return _AnswerGateResult("disclose", build_gap_disclosure(result))


def _tool_names_from_schemas(tools: list | None) -> list[str]:
    """Extract the ``name`` field of each tool schema in OpenAI format.

    Used at every chat-runtime call site to feed ``_compute_tool_choice``
    the list of tools the agent has been granted.  Pure, side-effect free
    — safe to call inside the hot loop.
    """
    if not tools:
        return []
    out: list[str] = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        func = t.get("function") or {}
        name = func.get("name", "")
        if name:
            out.append(str(name))
    return out


# ── Goal-Contract live table-coverage probe ──────────────────────────
# The zero-row remediation message tells the model to "probe MAX(date) per
# candidate table".  When exactly ONE bound database KB exists, we can run
# that probe OURSELVES (cheap COUNT(*)) and hand the model concrete facts
# instead of a generic instruction.  Multi-KB turns are ambiguous (we don't
# track which KB a given SQL ran against) → return None and fall back to
# the static hint text.
_GC_SAFE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _make_goal_contract_table_executor(
    db: object,
    kb_ids: list[str] | None,
    timeout_s: int = 3,
) -> object | None:
    """Build a live ``table -> coverage`` probe for goal-contract feedback.

    Runs ``SELECT COUNT(*) FROM <table>`` against the FIRST bound KB and
    returns a human summary like ``"12 rows"``, or ``""`` on any failure.
    Results are memoized per table so repeated ``unmet()`` checks within a
    turn don't hammer the database.  Returns ``None`` when the probe target
    is ambiguous (0 or >1 bound KBs) or the KB id is missing — callers then
    degrade to the static hint text.
    """
    if not kb_ids or len(kb_ids) != 1:
        return None
    kb_id = kb_ids[0]
    if not kb_id:
        return None
    cache: dict[str, str] = {}

    def _probe(table: str) -> str:
        if not table:
            return ""
        if table in cache:
            return cache[table]
        # Defense in depth: only plain identifiers may be interpolated.
        if not _GC_SAFE_IDENT.match(table):
            cache[table] = ""
            return ""
        cov = ""
        try:
            from app.services.db.query_service import QueryService

            svc = QueryService(db)
            res = svc.execute(
                kb_id,
                f"SELECT COUNT(*) AS __gc_cnt FROM {table}",
                max_rows=1,
                timeout_s=timeout_s,
            )
            rows = res.get("rows") or []
            cnt = rows[0].get("__gc_cnt") if rows else 0
            if cnt is not None:
                cov = f"{int(cnt)} row(s)"
        except Exception as exc:  # noqa: BLE001 — coverage is best-effort
            logger.info("goal-contract table probe failed for %s: %s", table, exc)
            cov = ""
        cache[table] = cov
        return cov

    return _probe


def _compute_tool_choice(
    user_message: str,
    data_ctx_extras: dict,
    iteration: int,
    tool_names: list[str] | None = None,
) -> dict | None:
    """Determine the tool_choice parameter for the LLM call.

    This is the single server-side enforcement point that turns a
    detected user intent (time-sensitive / URL / file-format / data
    question) into a hard ``tool_choice`` on iteration 0.  Without this,
    a weak function-calling model (e.g. ``deepseek-chat`` on
    ``tool_choice=auto``) reliably prefers prose over ``tool_calls`` —
    producing the "I'll create…" / hallucinated-news symptoms observed
    in production.

    Precedence (highest first) — see
    :func:`app.services.turn_action.resolve_turn_action`:

      1. ``ask_data_agent``  — bound KBs + data question (existing path)
      2. ``create_artifact`` — file-format intent (``docx``/``pptx``/...)
      3. ``web_extract``     — URL present in the message
      4. ``web_search``      — time-sensitive keyword heuristic
      5. ``None``            — general chitchat; ``tool_choice=auto``

    Forcing only applies on ``iteration == 0`` so multi-step tool loops
    are not interfered with.  Tool presence is required to force — we
    never ask the LLM to call a tool the agent has not been granted.

    Args:
        user_message: The original user message.
        data_ctx_extras: Context dict with ``bound_kb_ids`` and any
                         other data-source runtime fields.
        iteration: Current loop iteration (0-based).
        tool_names: Names of tools granted to the agent. Optional for
                    backward compatibility; when ``None``, no tool is
                    forced (block is still injected as a soft nudge).

    Returns:
        - ``None`` for "auto" (default LLM behavior).
        - ``{"type": "function", "function": {"name": "<tool>"}}`` to
          force the named tool on this iteration.
    """
    # Lazy import to avoid a circular dependency at module load time
    # (turn_action imports from agent_prompts, but agents.py is already
    # importing from agent_prompts — a top-level import would still
    # work, but lazy import makes the seam explicit and keeps
    # ``_compute_tool_choice`` testable in isolation).
    from app.services.turn_action import resolve_turn_action

    action = resolve_turn_action(
        user_message=user_message,
        tool_names=tool_names or [],
        data_ctx_extras=data_ctx_extras or {},
        is_data_question=_is_data_question(user_message),
        iteration=iteration,
    )
    if not action.forced_tool:
        return None
    logger.info(
        "tool_choice forcing: %s (iteration=%d)",
        action.forced_tool, iteration,
    )
    return {"type": "function", "function": {"name": action.forced_tool}}


def _finish_line_tool_choice(
    iteration: int,
    final_iteration: int,
    dashboard_forced: bool,
    tool_choice: dict | None,
) -> dict | str | None:
    """Final-iteration override: force ``tool_choice="none"`` so the LLM must
    produce a text answer instead of issuing yet another tool call.

    This is the "guaranteed final answer" finish line. When the loop reaches
    its last iteration, the LLM has gathered everything it is going to gather;
    telling it ``tool_choice="none"`` forces it to synthesize a prose answer
    from whatever tool results are already in the message history, instead of
    one more exploratory call that would exhaust the budget and dump us into
    the generic empty-content fallback.

    Precedence (highest first):

      1. Dashboard-guard forcing — calling ``create_dashboard`` IS the finish
         line for dashboard requests; it always wins over ``"none"`` so a
         dashboard turn ends by producing the artifact, not prose.
      2. Existing ``tool_choice`` — a forced tool (``ask_data_agent`` /
         ``create_artifact`` / ``web_extract`` / ``web_search``) computed for
         this iteration is preserved on non-final iterations.
      3. Forced ``"none"`` — only on the FINAL iteration, and only when the
         dashboard guard did not already force ``create_dashboard``.

    Args:
        iteration: Current (0-based) loop variable.
        final_iteration: Last value the loop variable will take (exclusive
            bound minus one).
        dashboard_forced: True when the dashboard guard forced
            ``create_dashboard`` this iteration.
        tool_choice: The ``tool_choice`` computed so far this iteration
            (``None`` means "auto", a dict means a forced function).

    Returns:
        ``"none"`` on the final iteration (unless dashboard-forced), else the
        incoming ``tool_choice`` unchanged.
    """
    return maybe_force_finish_line(
        iteration, final_iteration, dashboard_forced, tool_choice
    )


@router.get("/agents/runtime")
async def list_runtime_agents(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Platform-admin diagnostic: list all automation runtime agents.

    Returns id, org_id, app_id, status, created_date for each hidden
    runtime agent. Gated by ``user.role == "admin"``.
    """
    if getattr(user, "role", "user") != "admin":
        raise HTTPException(status_code=403, detail="admin only")
    agents = db.query(AgentApp).filter(
        AgentApp.role == "automation_runtime",
        AgentApp.is_deleted == False,  # noqa: E712
    ).all()
    return [
        {
            "id": a.id,
            "org_id": a.org_id,
            "app_id": a.app_id,
            "status": a.status,
            "created_date": a.created_date.isoformat() if a.created_date else None,
        }
        for a in agents
    ]


@router.get("/apps/{app_id}/agents/conversations")
async def list_conversations(
    app_id: str,
    agent_name: str | None = Query(None),
    limit: int | None = Query(None),
    skip: int | None = Query(None),
    sort_by: str | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """List agent conversations, most recent first.

    Optional ``agent_name`` query param filters by agent (e.g. ``skill_agent``,
    ``agent_builder``).  This is used by the SDK's ``listConversations({agent_name})``.

    Owner-scoped: a non-admin user only sees conversations they created.
    """
    query = db.query(AgentConversation).filter(AgentConversation.is_deleted == False)
    if user.role != "admin":
        query = query.filter(AgentConversation.created_by_id == user.id)
    if agent_name:
        query = query.filter(AgentConversation.agent_name == agent_name)
    query = query.order_by(AgentConversation.created_date.desc())
    if skip:
        query = query.offset(skip)
    if limit:
        query = query.limit(limit)
    return [r.to_dict() for r in query.all()]


@router.post("/apps/{app_id}/agents/conversations")
async def create_conversation(
    app_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Create a new agent conversation.

    The frontend sends ``{agent_name, metadata: {name, description}}``.
    We store ``metadata`` in the ``metadata_`` column and also copy
    ``metadata.name`` to ``title`` for display.
    """
    created_by = user.id if user else None
    metadata = body.get("metadata")
    title = body.get("title", "New Conversation")
    if metadata and isinstance(metadata, dict) and metadata.get("name"):
        title = metadata["name"]
    # The frontend (createAgentConversation in agentEnhanced.js) sends
    # ``project_id`` inside ``metadata`` rather than at the top level.
    # Accept both so the project scoping reaches the data-source runtime.
    project_id = body.get("project_id")
    if not project_id and isinstance(metadata, dict):
        project_id = metadata.get("project_id")
    # Validate ``project_id`` against the live ``projects`` table. The
    # frontend caches project context in ``sessionStorage['zhanlu:lastProjectContext']``
    # and can outlive the actual project — e.g. an admin deletes the
    # project (or it was hard-deleted) but the user's browser still
    # holds the stale UUID. Sending that into the FK column surfaces
    # as a 500 IntegrityError and the user sees "agents are not
    # responding" with no clue why. Fall back to ``None`` so the
    # conversation is created against the agent's default data-source
    # scope (graceful degradation rather than a hard failure).
    if project_id:
        from app.models.project import Project as _Project  # local import: avoids module-load cycles
        _project_row = (
            db.query(_Project.id)
            .filter(_Project.id == project_id, _Project.is_deleted == False)  # noqa: E712
            .first()
        )
        if _project_row is None:
            # Stale or non-existent project id — drop it. Also strip
            # ``project_id``/``project_name`` from the stored metadata
            # so the conversation isn't tagged with a phantom project.
            project_id = None
            if isinstance(metadata, dict):
                metadata = {k: v for k, v in metadata.items() if k not in ("project_id", "project_name", "project")}
    conv = AgentConversation(
        agent_name=body.get("agent_name"),
        title=title,
        messages=[],
        status="active",
        created_by_id=created_by,
        # Tag the conversation with the project the user selected so the
        # data-source runtime scopes to that project's KBs + context.
        # If validation above dropped a stale project_id, this stays None.
        project_id=project_id,
    )
    conv.metadata_ = metadata
    db.add(conv)
    db.commit()
    db.refresh(conv)
    return conv.to_dict()


@router.get("/apps/{app_id}/agents/conversations/{conversation_id}")
async def get_conversation(
    app_id: str,
    conversation_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Get a conversation by ID, including all messages."""
    conv = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.is_deleted == False,
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.created_by_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to access this conversation")
    return conv.to_dict()


@router.put("/apps/{app_id}/agents/conversations/{conversation_id}")
async def update_conversation(
    app_id: str,
    conversation_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Update a conversation's metadata or status.

    Used by the frontend to soft-delete conversations via
    ``updateConversation(id, {metadata: {_deleted: true}})``.
    """
    conv = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.is_deleted == False,
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.created_by_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to modify this conversation")

    if "metadata" in body:
        conv.metadata_ = body["metadata"]
        md = body["metadata"]
        if isinstance(md, dict) and md.get("_deleted"):
            conv.is_deleted = True
    if "title" in body:
        conv.title = body["title"]
    if "status" in body:
        conv.status = body["status"]
    conv.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)
    return conv.to_dict()


@router.put("/apps/{app_id}/agents/conversations/{conversation_id}/permission-mode")
async def set_permission_mode(
    app_id: str,
    conversation_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Set the permission mode for a specific conversation.

    Stores the mode in ``conv.metadata_["permission_mode"]`` so it
    overrides the agent-level and default permission modes for all
    subsequent tool calls in this conversation.

    Body: ``{"mode": "default" | "plan" | "full_auto"}``
    """
    mode = body.get("mode")
    if mode not in ("default", "plan", "full_auto"):
        raise HTTPException(
            status_code=400,
            detail="mode must be one of: default, plan, full_auto",
        )

    conv = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.is_deleted == False,
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.created_by_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to modify this conversation")

    conv.metadata_ = conv.metadata_ or {}
    conv.metadata_["permission_mode"] = mode
    conv.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)

    logger.info(
        "Conversation %s permission mode set to '%s'",
        conversation_id, mode,
    )
    return {"success": True, "conversation_id": conversation_id, "permission_mode": mode}


@router.post("/apps/{app_id}/agents/conversations/v2/{conversation_id}/messages")
async def add_message(
    app_id: str,
    conversation_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Add a message to a conversation and run the agent runtime.

    The SDK calls this with {role: "user", content: "..."}.
    The runtime calls the LLM with tools, executes tool calls, and
    returns the updated conversation with structured messages.

    The assistant message may include a ``tool_calls`` array — each element
    has ``name``, ``arguments_string``, ``results``, and ``status``. The
    frontend uses these to detect agent creation and display tool execution.
    """
    conv = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.is_deleted == False,
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.created_by_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to access this conversation")

    # Wire the authenticated user's role into the request-scoped trace
    # context so RBAC tool-filtering in tool_registry.get_schemas() (which
    # reads TraceContext.current_role()) activates for this request.
    TraceContext.set(
        session_id=str(conv.id),
        user_id=str(user.id),
        agent_name=conv.agent_name,
        role=user.role,
    )

    messages = conv.messages or []
    user_content = body.get("content", "")
    user_role = body.get("role", "user")
    # 2026-08-25: auto-rebind conversation to the dedicated system agent
    # when the user pastes an automation-setup / agent-creation /
    # skill-creation template (EN/ZH/structural), so the LLM uses the
    # correct toolset instead of misfiring on ask_data_agent.
    _route_to_dedicated_system_agent(conv, user_content, user_role, db)
    user_model_v2: str | None = body.get("model")
    # FIX 2026-08-23: extract selected skill from chip so
    # _build_selected_skill_runtime_block can inject SKILL.md
    selected_skill_v2 = body.get("selected_skill") if isinstance(body.get("selected_skill"), dict) else None
    selected_skill_id_v2 = body.get("selected_skill_id") or (selected_skill_v2 or {}).get("id")
    if selected_skill_v2 and selected_skill_id_v2 and not selected_skill_v2.get("id"):
        selected_skill_v2["id"] = selected_skill_id_v2

    # ── Hierarchical LLM resolution ─────────────────────────────────
    # Honor the per-message body project_id (the frontend sends the
    # live-URL selection on every message). A conv created without a
    # project (legacy rows, main-chat entry) must still follow the
    # selected project's configured LLM — otherwise the agent reads
    # the project's data sources but thinks with the default model.
    _llm_pid_v2 = resolve_message_project_id(
        db,
        conv_project_id=getattr(conv, "project_id", None),
        body_project_id=body.get("project_id"),
        body_project_name=body.get("project_name"),
    )
    if _llm_pid_v2 and not getattr(conv, "project_id", None):
        # One-time heal: persist the binding so later turns and the
        # project's Recent Chats list see this conversation.
        conv.project_id = _llm_pid_v2
    _eff_llm_v2 = resolve_effective_llm(
        db,
        project_id=_llm_pid_v2,
        agent_name=conv.agent_name,
        user_model=user_model_v2,
        user_is_admin=(user.role == "admin"),
        org_id=conv.org_id,
        app_id=conv.app_id,
    )
    if _eff_llm_v2.locked and user_model_v2:
        logger.info(
            "LLM locked by admin config (reason=%s) — ignoring user model %s",
            _eff_llm_v2.locked_reason, user_model_v2,
        )

    # ── User-level LLM overrides (Settings page) ──────────────────────
    # Computed once per-request so all LLM calls in this handler (primary,
    # retry, compaction retry, fallback) use the same user preferences.
    # Per-request explicit values in ``body`` always win; these are only
    # defaults read from ``user_settings``.
    llm_overrides = get_user_llm_overrides(db, user.id)

    # Add the user message
    user_msg = {
        "id": str(uuid.uuid4()),
        "role": user_role,
        "content": user_content,
        "created_date": datetime.now(timezone.utc).isoformat(),
    }
    messages.append(user_msg)

    # --- Persist the user message immediately ---
    # CRITICAL: commit the user message to the DB BEFORE the tool-calling
    # loop starts. Without this, a crash inside the agent loop (LLM
    # error, network drop, server kill) leaves the user staring at an
    # empty chat with their message lost — the "not showing anything"
    # symptom. By committing here, the user message is durable and will
    # appear in the conversation history on reload, even if the rest
    # of the agent run never completes.
    conv.messages = list(messages)
    conv.updated_date = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception as _early_commit_err:
        logger.warning(
            "add_message: early user-message commit failed: %s",
            _early_commit_err,
        )
        db.rollback()
        # Don't fail the request — the loop's own error handlers can
        # still attempt to persist. Log loudly so we notice.

    # If this is a user message, run the agent runtime
    # NOTE: agent_app and agent_app_id are bound at function-body scope (below)
    # so the post-loop memory extraction at the end of this function can
    # always reference them without raising UnboundLocalError — even when
    # the incoming message has a non-user role or empty content.
    agent_name = conv.agent_name
    agent_app = None
    tool_config = None
    if agent_name:
        agent_app = db.query(AgentApp).filter(
            AgentApp.name == agent_name,
            AgentApp.is_deleted == False,
        ).first()
        if agent_app and agent_app.tool_config:
            tool_config = agent_app.tool_config
        elif agent_app:
            # P8: Composable toolsets -- resolve from posture when no explicit config
            try:
                from app.services.composable_toolsets import resolve_from_agent_config
                _posture = getattr(settings, "DEFAULT_TOOLSET_POSTURE", "coding")
                _tools = resolve_from_agent_config(None, posture=_posture, skills=getattr(agent_app, "skills", None))
                tool_config = {"enabled_tools": _tools}
            except Exception:
                pass
    agent_app_id = agent_app.id if agent_app else agent_name

    # Resolve the chat session that owns this conversation, if any. We do a
    # reverse lookup because the LLM cannot pass the chat session id in its
    # tool-call args (it doesn't know it). The result is injected into the
    # tool context so ``_create_automation`` can link the new AutomationTask
    # back to the chat it was created from — without this link, the
    # Manus-style "Scheduled" button never appears in the chat header.
    chat_session_id: str | None = None
    try:
        from app.models.chat_session import ChatSession
        sess_row = db.query(ChatSession).filter(
            ChatSession.conversation_id == conv.id,
            ChatSession.is_deleted == False,  # noqa: E712
        ).order_by(ChatSession.created_date.desc()).first()
        if sess_row:
            chat_session_id = sess_row.id
    except Exception as _cs_err:
        logger.debug("add_message: chat_session lookup skipped: %s", _cs_err)

    if user_role == "user" and user_content:
        # --- Planning-layer routing (v2) ---
        # Classify the user message; when the classifier fires AND the
        # SynexiaFSM feature flag is on, run the cognitive loop and return
        # its result instead of the raw tool loop. Best-effort: a
        # classifier/FSM failure must NEVER block the chat path.
        #
        # Follow-up override: build the conversation context once here. If
        # the message is a short refinement of a prior turn ("make it dark
        # theme") the planning trigger's simple-conversation bypass would
        # normally route it to the context-blind legacy loop. Detect that
        # case and force the trigger True so the FSM (which has full
        # follow-up wiring) handles it. _conv_ctx is reused downstream by
        # both the FSM (via ExecutionRequest) and the legacy loop's system
        # prompt — no second DB query.
        _conv_ctx = None
        try:
            _conv_ctx = build_conversation_context(db, conversation_id, agent_name or "general_assistant")
        except Exception as _ctx_err:
            logger.debug("v2 build_conversation_context failed (non-fatal): %s", _ctx_err)

        try:
            _plan_trigger = should_trigger_planning(user_content)
            if not _plan_trigger and is_followup_refinement(user_content, _conv_ctx):
                logger.info(
                    "v2 add_message: follow-up override — routing refinement "
                    "turn to SynexiaFSM (conv=%s)",
                    conversation_id,
                )
                _plan_trigger = PlanTrigger(True, 1.0, {"followup": 1}, source="followup-override")
            # Data-bound override (same logic as v3 path): when the agent
            # has knowledge bases bound, OR the conversation has a pinned
            # data source, route through SynexiaFSM so quality gates are
            # enforced. Non-data intents (greeting/thanks) stay on the
            # direct path.
            _v2_bound_count = len(agent_app.knowledge_bases) if (agent_app and agent_app.knowledge_bases) else 0
            _v2_pinned = bool((conv.metadata_ or {}).get("data_source_id")) if conv else False
            _v2_has_data_ctx = _v2_bound_count > 0 or _v2_pinned
            if not _plan_trigger and _v2_has_data_ctx and not _is_non_data_intent(user_content):
                logger.info(
                    "v2 add_message: data-bound override — bound=%d pinned=%s, "
                    "routing to SynexiaFSM (conv=%s)",
                    _v2_bound_count, _v2_pinned, conversation_id,
                )
                _plan_trigger = PlanTrigger(True, 1.0, {"data_bound": _v2_bound_count, "pinned": int(_v2_pinned)}, source="data-bound-override")
            if _plan_trigger and is_fsm_enabled():
                logger.info(
                    "v2 add_message: planning trigger fired "
                    "(confidence=%.2f, signals=%s) — routing to SynexiaFSM "
                    "(conv=%s, user=%s)",
                    _plan_trigger.confidence,
                    _plan_trigger.signals,
                    conversation_id,
                    getattr(user, "id", None),
                )
                _fsm_result = SynexiaFSM(db).run(
                    ExecutionRequest(
                        conversation_id=conversation_id,
                        agent_name=agent_name or "general_assistant",
                        user_message=user_content,
                        user_id=str(getattr(user, "id", None)) if user else None,
                        mode="dynamic",
                        org_id=getattr(conv, "org_id", None) or "default-org",
                        app_id=app_id or "default-app",
                        conversation_context=_conv_ctx,
                        # Hierarchical LLM pin: FSM internals use the
                        # project/agent resolved endpoint, not .env defaults.
                        endpoint=_eff_llm_v2.endpoint,
                    )
                )
                # Persist the assistant reply into the conversation so the
                # caller sees it just like a normal tool-loop turn.
                _assistant_msg = {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": _fsm_result.assistant_content or "",
                    "created_date": datetime.now(timezone.utc).isoformat(),
                    "tool_calls": _fsm_result.tool_calls,
                }
                messages.append(_assistant_msg)
                conv.messages = list(messages)
                conv.updated_date = datetime.now(timezone.utc)
                try:
                    db.commit()
                except Exception as _fsm_commit_err:
                    logger.warning("v2 FSM commit failed (non-fatal): %s", _fsm_commit_err)
                    db.rollback()
                try:
                    db.refresh(conv)
                except Exception:
                    pass
                return conv.to_dict()
            else:
                logger.debug(
                    "v2 add_message: planning trigger did not fire "
                    "(confidence=%.2f, signals=%s, fsm_enabled=%s)",
                    _plan_trigger.confidence,
                    _plan_trigger.signals,
                    is_fsm_enabled(),
                )
        except Exception as _plan_err:
            # Classifier or FSM construction failed — fall through to the
            # existing tool loop. Never block chat on planning-layer errors.
            logger.warning(
                "v2 add_message: planning trigger check failed (non-fatal): %s",
                _plan_err,
            )

        # Build system prompt with memory + todo injection. Pass
        # user_content so the hard-MUST grounding heuristic can append a
        # [GROUNDING REQUIRED] block for time-sensitive questions.
        system_prompt = get_system_prompt(agent_name, agent_app, user_message=user_content)

        # P8: Unified system prompt assembly (memory + todos + coding context + learning graph)
        try:
            from app.services.dynamic_prompt_builder import build_system_prompt as _build_prompt
            system_prompt = _build_prompt(
                base_prompt=system_prompt,
                db=db,
                agent_app_id=agent_app_id,
                conversation_id=conversation_id,
                user_id=user.id if user else None,
                agent_app=agent_app,
                # Project scope (2026-08-05): the same `sel_proj_name`
                # resolved above for the data-source runtime is the
                # right project_id to forward to the memory snapshot.
                # Without this, the agent recalls agent notes (e.g.
                # the "Q2 2026 sales report" entry) from every
                # project the user has ever visited — the user sees
                # the same report-recollection greeting in convs
                # across all projects. Memory writes are stamped
                # with the same project_id at extraction time so the
                # notes stay inside the project they were taken in.
                project_id=getattr(conv, "project_id", None),
            )
        except Exception:
            pass  # Prompt building is best-effort

        # Inject conversation context (follow-up awareness) so the legacy
        # loop resolves refinement turns ("make it dark theme") against
        # prior artifacts instead of treating them as brand-new topics.
        try:
            _followup_block = format_followup_context_block(_conv_ctx)
            if _followup_block:
                system_prompt += _followup_block
        except Exception:
            pass  # Follow-up context injection is best-effort

        # Inject skill methodology content (from .md skill files) so the
        # agent knows how to use its bound skills. Best-effort — skipped if
        # the skills_loader hasn't loaded any skills or the agent has none.
        #
        # Progressive-disclosure branch: when true, inject only metadata
        # (name + description + summary) so the agent calls `load_skill_body`
        # to fetch full bodies on demand.  When false (legacy), inject the
        # full `skill_md` body directly.
        if agent_app and getattr(agent_app, "skills", None):
            try:
                use_progressive = getattr(agent_app, "progressive_disclosure", False)
                if use_progressive:
                    from app.services.skills_loader import get_skill_metadata_for_agent
                    skill_prompt = get_skill_metadata_for_agent(agent_app.skills, db=db)
                else:
                    from app.services.skills_loader import get_skill_prompt_for_agent
                    skill_prompt = get_skill_prompt_for_agent(agent_app.skills, db=db)
                if skill_prompt:
                    system_prompt += f"\n\n{skill_prompt}"
            except Exception:
                pass  # Skill injection is best-effort

        # Full skill catalog context injection (dynamic discovery + routed
        # skill). Every agent sees ALL available skills — not just its
        # bound ones — so it can autonomously discover and use any skill.
        # The block is assembled by skill_routing.runtime_catalog:
        #   • truthful tool instructions (only tools that actually exist —
        #     FIX 2026-08-29: the old text promised `load_skill_body` /
        #     `Skill`, which were not registered → "Unknown tool" failures)
        #   • relevance-forced catalog (search hits + routed skill survive
        #     the character budget even when it truncates 894 → ~52)
        #   • auto-routed skill body (format_intent/soft_intent → full
        #     SKILL.md directive, so methodology is followed WITHOUT the
        #     model having to discover + load the skill itself)
        #   • context-scaled budget (15K..40K chars by model window)
        # Best-effort, token-budgeted.
        if agent_app:
            try:
                from app.services.skill_routing.runtime_catalog import (
                    build_skill_catalog_context,
                )
                _explicit_skill_name = None
                if isinstance(selected_skill_v2, dict):
                    _explicit_skill_name = selected_skill_v2.get("name")
                elif isinstance(selected_skill_v2, str):
                    _explicit_skill_name = selected_skill_v2
                _skill_ctx_block = build_skill_catalog_context(
                    user_content,
                    db,
                    bound_skills=set(agent_app.skills or []),
                    context_window_tokens=(
                        _eff_llm_v2.endpoint.context_window
                        if _eff_llm_v2.endpoint else None
                    ),
                    explicit_skill_name=_explicit_skill_name,
                )
                if _skill_ctx_block:
                    system_prompt += _skill_ctx_block
            except Exception:
                pass  # Catalog injection is best-effort

            # FIX 2026-08-23: inject the user-selected skill's SKILL.md
            # body as a hard runtime directive so the LLM follows it.
            _sel_skill_block = _build_selected_skill_runtime_block(
                db, selected_skill_v2, selected_skill_id_v2,
            )
            if _sel_skill_block:
                system_prompt += "\n\n" + _sel_skill_block
                logger.info(
                    "v2 selected_runtime_skill injected for conv=%s, skill_id=%s",
                    conversation_id, selected_skill_id_v2,
                )

        # Get tool definitions (uses registry if tool_config is set)
        tools = get_tools(agent_name, tool_config, agent_app)
        # Pin web_search to the front of the tool list when the user
        # message is time-sensitive (hard-MUST grounding enforcement).
        if user_content:
            tools = apply_grounding_to_schemas(tools, user_content)

        # Auto-inject DB tools + "Bound Data Sources" prompt section if the
        # agent has data sources. Idempotent — no-op when nothing is bound.
        from app.services.compaction import get_context_window
        try:
            from app.services.data_source_runtime import prepare_data_source_runtime
            from app.models.project import Project as _Project
            _sel_proj_name = None
            if getattr(conv, "project_id", None):
                _sel_proj_row = db.get(_Project, conv.project_id)
                _sel_proj_name = _sel_proj_row.name if _sel_proj_row else None
            tools, system_prompt, data_ctx_extras = prepare_data_source_runtime(
                db, agent_app, tools, system_prompt,
                selected_project_id=getattr(conv, "project_id", None),
                selected_project_name=_sel_proj_name,
                user_id=user.id if user else None,
                user_message=user_content,
                # P1-5: pass the user-selected model's context window so
                # the Bound Data Sources block auto-compresses for small
                # models (e.g. qwen3.6-27b 65k) and stays full for big
                # models (deepseek 128k, claude 200k).  Uses the REAL
                # resolved window (admin-set or auto-probed) so ANY model —
                # not just names in the heuristic — gets the right budget.
                target_context_window=get_context_window(
                    user_model_v2 or get_model(),
                    context_window_tokens=(
                        _eff_llm_v2.endpoint.context_window
                        if _eff_llm_v2.endpoint else None
                    ),
                ),
            )
        except Exception as e:
            logger.debug("Data source runtime prep failed (non-fatal): %s", e)
            data_ctx_extras = {}

        # Dynamic tool loading (2026-08-31): always-on core + intent-relevant
        # periphery for THIS turn. Fail-open: any error returns the full list.
        try:
            from app.services.dynamic_tools import select_tools_for_turn

            tools = select_tools_for_turn(tools, user_content)
        except Exception as _dt_err:
            logger.debug("v2 dynamic tool loading skipped (non-fatal): %s", _dt_err)

        # Build LLM message history
        llm_messages = [{"role": "system", "content": system_prompt}]
        for msg in messages:
            role = msg.get("role")
            if role in ("user", "assistant"):
                content = msg.get("content", "")
                if content:
                    llm_messages.append({"role": role, "content": content})

        # --- Auto-Compaction: check if context needs compression ---
        # P8: Pluggable context engine — when CONTEXT_ENGINE != "default",
        # use the registered engine instead of the inline compaction code.
        _engine_name = getattr(settings, "CONTEXT_ENGINE", "default")
        _engine_handled = False
        if _engine_name != "default":
            try:
                from app.services.context_engine import get_context_engine
                # FIX 2026-08-24 (v2): the engine's threshold check uses
                # the user's model (so e.g. qwen3.6-27b's 65k window is
                # respected), but the actual LLM-based compression MUST use
                # a model with enough context to fit a 60k+ token input
                # (deepseek-v4-flash, 128k).
                _ctx_engine = get_context_engine(_engine_name, model=user_model_v2 or get_model())
                if _ctx_engine.should_compress(llm_messages):
                    logger.info("Context engine '%s' compacting conversation %s", _engine_name, conversation_id)
                    llm_messages, _wc = await _ctx_engine.compress(llm_messages, model=get_model())
                    if _wc and (not llm_messages or llm_messages[0].get("role") != "system"):
                        llm_messages.insert(0, {"role": "system", "content": system_prompt})
                _engine_handled = True
            except Exception as e:
                logger.warning("Context engine '%s' failed, falling back to default: %s", _engine_name, e)

        # Existing inline compaction (runs as no-op when engine already handled it)
        try:
            from app.services.compaction import (
                AutoCompactState,
                auto_compact_if_needed,
                should_autocompact,
                estimate_messages_tokens,
            )
            # FIX 2026-08-24 (v2): use the USER'S selected model ONLY for
            # the threshold check (so e.g. qwen3.6-27b's 65,536 window is
            # respected), but the compaction's LLM call MUST use a model
            # with a large enough context to actually summarize 60k+ tokens.
            # qwen3.6-27b (65k) can't summarize 60k+ tokens of itself, so
            # the compactor falls back to the global default
            # (deepseek-v4-flash, 128k) for the LLM step.
            model_name = user_model_v2 or get_model()
            compactor_model = get_model()  # LLM that actually summarizes
            state = _compaction_states.get(conversation_id)
            if state is None:
                state = AutoCompactState()
                _compaction_states[conversation_id] = state

            if should_autocompact(llm_messages, model_name, state):
                logger.info("Auto-compacting conversation %s (token estimate high)", conversation_id)
                llm_messages, was_compacted = await auto_compact_if_needed(
                    llm_messages,
                    model=compactor_model,
                    state=state,
                    trigger="auto",
                )
                if was_compacted:
                    # Ensure system prompt is still first
                    if not llm_messages or llm_messages[0].get("role") != "system":
                        llm_messages.insert(0, {"role": "system", "content": system_prompt})
                    logger.info("Compaction done, messages now: %d", len(llm_messages))
        except Exception as e:
            logger.warning("Compaction check failed (non-fatal): %s", e)
        # --- End Auto-Compaction ---

        # Multi-turn tool calling loop
        assistant_msg_id = str(uuid.uuid4())
        artifact_ids: list[str] = []
        tool_calls_for_frontend = []
        guardrail_retries = 0
        # File-format intent is needed INSIDE the loop (synthesis gate for
        # the empty-rows path) as well as after it (orchestrator fallback),
        # so compute it once up front.
        _orch_doc_format = detect_file_intent(user_content)
        # P0 reliability: per-turn guardrail controller + per-conversation iteration budget
        guard_ctrl = ToolLoopGuardController(_loop_guard_config_for(agent_app))
        _max_iters = getattr(agent_app, "max_call_count", None) or settings.AGENT_MAX_ITERATIONS
        conv_budget = IterationBudget(max_total=_max_iters)
        _verify_attempts = 0
        _gate_attempts = 0  # universal self-eval re-plan nudges this turn
        _pptx_nudge_attempts = 0  # pptx turn-guard nudges this turn
        # Fix 1b: one-shot "force create_artifact next iteration" signal set
        # by the LAST allowed pptx nudge (pptx_turn_guard.force_next). Read at
        # the top of the next iteration next to should_force_create_pptx.
        _pptx_force_next_iteration = False
        # File-deliverable turn guard variables (mirrors pptx).
        _file_nudge_attempts = 0
        _file_force_next_iteration = False
        # Finish-line state (UnboundLocalError discipline: initialized BEFORE
        # the loop so any post-loop read is safe). ``dashboard_forced`` tracks
        # whether the dashboard guard forced ``create_dashboard`` this turn;
        # ``_wrapup_nudged`` ensures the T-3 wrap-up message is injected once.
        dashboard_forced = False
        _wrapup_nudged = False
        for iteration in range(MAX_TOOL_ITERATIONS):
            # P0: consume one iteration from the conversation-level budget
            if not conv_budget.consume():
                logger.info(
                    "Conversation %s iteration budget exhausted (%d/%d), breaking",
                    conversation_id, conv_budget.used, conv_budget.max_total,
                )
                break
            # Per-tool-name runaway guard. If the LLM has called the same
            # tool TOOL_CALL_HARD_CAP times already, inject a final nudge
            # and break — do NOT make another LLM call. This stops the
            # classic skills/skills_hub loop on agent_builder.
            loop_info = _detect_tool_call_loop(llm_messages)
            if loop_info is not None:
                looped_tool, looped_n = loop_info
                # Internal LLM-facing nudge: tells the model to stop
                # calling the same tool and wrap up. This text is
                # scaffolding for the model, not for the user.
                nudge = (
                    f"Tool '{looped_tool}' was already called {looped_n} times. "
                    "Use the result you have and produce your final answer. "
                    "Do not call it again."
                )
                # User-facing assistant content: a short, friendly
                # message that does NOT mention internal tool names or
                # call counts. The previous implementation reused
                # `nudge` for both, which leaked the internal scaffolding
                # into the chat UI as a visible assistant message.
                # After R4 we explicitly tell the user we are proceeding
                # with sensible defaults so the loop guard stopping the
                # agent before create_agent does not feel like a dead end.
                assistant_content = (
                    "I'm going to build the agent with sensible defaults now. "
                    "You can adjust anything after creation."
                )
                logger.warning(
                    "Tool-call loop guard tripped in conversation %s: "
                    "tool=%r count=%d (cap=%d). Breaking loop.",
                    conversation_id, looped_tool, looped_n, TOOL_CALL_HARD_CAP,
                )
                llm_messages.append({"role": "user", "content": nudge})
                break

            # Finish line: with 3 iterations left, tell the model to stop
            # exploring and assemble its final answer (injected exactly once).
            if iteration == MAX_TOOL_ITERATIONS - 3 and not _wrapup_nudged:
                _wrapup_nudged = True
                llm_messages.append({
                    "role": "user",
                    "content": (
                        "You have 3 steps left. Stop exploring and produce "
                        "your final answer with what you have."
                    ),
                })
            # Compute tool_choice for this iteration.  On iteration 0 the
            # server enforces the matching tool (ask_data_agent /
            # create_artifact / web_extract / web_search) so a weak
            # function-calling model cannot simply answer from training
            # memory.  See ``_compute_tool_choice`` for the precedence.
            tool_choice = _compute_tool_choice(
                user_content, data_ctx_extras, iteration,
                tool_names=_tool_names_from_schemas(tools),
            )

            # Dashboard guard: same logic as the v3 stream — if the user
            # asked for a live dashboard and we've already done the
            # schema/design pass, force the next LLM turn to call
            # `create_dashboard`. Without this, the agent dumps markdown
            # tables instead of producing an interactive dashboard artifact.
            # Also fires in dashboard-capable projects when the agent
            # speculatively loads
            # dashboard skills before being asked — the user just said "hi"
            # but the agent is in a BI project with a bound datasource.
            _tool_names = _tool_names_from_schemas(tools)
            _is_dash_project = bool(agent_name and (
                "bi_assistant" in agent_name.lower()
            ))
            _dash_build_tool = dashboard_build_tool()
            if (
                _dash_build_tool in _tool_names
                and should_force_create_dashboard(
                    user_content,
                    tool_calls_for_frontend,
                    has_dashboard_tool=True,
                    is_dashboard_project=_is_dash_project,
                )
            ):
                logger.info(
                    "v2 stream: forcing %s after schema/design "
                    "pass (conv=%s, iter=%d, prior_tool_count=%d, dash_project=%s)",
                    _dash_build_tool, conversation_id, iteration,
                    len(tool_calls_for_frontend), _is_dash_project,
                )
                tool_choice = {
                    "type": "function",
                    "function": {"name": _dash_build_tool},
                }
                dashboard_forced = True
            # PPTX turn-guard: mirror the dashboard forcing for artifact decks.
            # When the user asked for a PPT and the tool-loop budget window is
            # closing with no pptx artifact created, force create_artifact so
            # the model must emit the call (dashboard forcing wins via the
            # dashboard_forced guard inside should_force_create_pptx).
            _pptx_forced = False
            # Fix 1b: the last-allowed synthesis-boundary nudge arms
            # _pptx_force_next_iteration, which overrides the T-window check
            # here so the model MUST emit create_artifact next turn.
            if _pptx_force_next_iteration or should_force_create_pptx(
                user_content,
                tool_calls_for_frontend,
                iteration=iteration,
                max_iterations=MAX_TOOL_ITERATIONS,
                has_artifact_tool="create_artifact" in _tool_names,
                dashboard_forced=dashboard_forced,
            ):
                logger.info(
                    "v2 stream: forcing create_artifact for pptx request "
                    "(conv=%s, iter=%d, prior_tool_count=%d)",
                    conversation_id, iteration, len(tool_calls_for_frontend),
                )
                tool_choice = {
                    "type": "function",
                    "function": {"name": "create_artifact"},
                }
                _pptx_forced = True
                _pptx_force_next_iteration = False  # consume the one-shot force
            # File-deliverable turn-guard: mirrors pptx for html/docx/pdf/xlsx/md.
            _file_forced = False
            if not _pptx_forced and not dashboard_forced and (
                _file_force_next_iteration or should_force_create_file(
                    user_content,
                    tool_calls_for_frontend,
                    iteration=iteration,
                    max_iterations=MAX_TOOL_ITERATIONS,
                    has_artifact_tool="create_artifact" in _tool_names,
                    dashboard_forced=dashboard_forced,
                    pptx_forced=_pptx_forced,
                )
            ):
                logger.info(
                    "v2 stream: forcing create_artifact for file deliverable "
                    "(conv=%s, iter=%d, prior_tool_count=%d)",
                    conversation_id, iteration, len(tool_calls_for_frontend),
                )
                tool_choice = {
                    "type": "function",
                    "function": {"name": "create_artifact"},
                }
                _file_forced = True
                _file_force_next_iteration = False
            # Finish line: on the final iteration force tool_choice="none" so
            # the LLM must answer in text. Guard forcing (dashboard + pptx + file) wins.
            tool_choice = _finish_line_tool_choice(
                iteration, MAX_TOOL_ITERATIONS - 1,
                dashboard_forced or _pptx_forced or _file_forced, tool_choice,
            )
            try:
                # P1.3: Pre-API deterministic tool result pruning (no LLM call)
                prune_tool_results_only(llm_messages, model=get_model())
                sanitize_messages(llm_messages)
                llm_response = await _call_llm_with_tools(
                    llm_messages, tools, tool_choice=tool_choice,
                    endpoint=_eff_llm_v2.endpoint,
                    temperature=llm_overrides.get("temperature"),
                    max_tokens=llm_overrides.get("max_tokens"),
                )
            except Exception as e:
                # P1.1: Structured API error classification
                ce = classify_api_error(e)
                logger.warning(
                    "LLM call error in conversation %s: reason=%s retryable=%s should_compress=%s err=%r",
                    conversation_id, ce.reason.value, ce.retryable, ce.should_compress, e,
                )
                metrics.record_error(ce.reason.value)
                if ce.should_compress:
                    # Context overflow -- attempt reactive compaction
                    logger.warning("Context overflow in conversation %s, attempting reactive compaction", conversation_id)
                    try:
                        from app.services.compaction import auto_compact_if_needed
                        llm_messages, was_compacted = await auto_compact_if_needed(
                            llm_messages,
                            # FIX 2026-08-24 (v2): the compactor's LLM call
                            # needs a LARGER context than the user's model
                            # when the conversation is already over the
                            # user's limit.  Use the global default
                            # (deepseek-v4-flash, 128k) to actually be able
                            # to summarize a 60k-token conversation even
                            # when the user is on qwen3.6-27b (65k).
                            model=get_model(),
                            state=_compaction_states.get(conversation_id, AutoCompactState()),
                            force=True,
                            trigger="reactive",
                        )
                        if was_compacted:
                            # Ensure system prompt is first
                            if not llm_messages or llm_messages[0].get("role") != "system":
                                llm_messages.insert(0, {"role": "system", "content": system_prompt})
                            logger.info("Reactive compaction done, retrying LLM call with %d messages", len(llm_messages))
                            # Retry the LLM call
                            prune_tool_results_only(llm_messages, model=get_model())
                            sanitize_messages(llm_messages)
                            llm_response = await _call_llm_with_tools(
                                llm_messages, tools, tool_choice=tool_choice,
                                endpoint=_eff_llm_v2.endpoint,
                                temperature=llm_overrides.get("temperature"),
                                max_tokens=llm_overrides.get("max_tokens"),
                            )
                        else:
                            assistant_content = f"Sorry, the conversation is too long and compaction failed: {str(e)}"
                            break
                    except Exception as compact_err:
                        logger.error("Reactive compaction failed: %s", compact_err)
                        assistant_content = f"Sorry, I encountered an error: {str(e)}"
                        break
                elif ce.should_fallback:
                    # P4: Try fallback models
                    async def _call_with_fallback_model(_model_name):
                        return await _call_llm_with_tools(
                            llm_messages, tools, tool_choice=tool_choice,
                            model_override=_model_name,
                            endpoint=_eff_llm_v2.endpoint,
                            temperature=llm_overrides.get("temperature"),
                            max_tokens=llm_overrides.get("max_tokens"),
                        )
                    _fb_response, _fb_model = await with_fallback(
                        get_model(), _call_with_fallback_model, ce,
                        user_fallback=llm_overrides.get("fallback_model"),
                    )
                    if _fb_response:
                        llm_response = _fb_response
                        logger.info("Fallback to model %s succeeded", _fb_model)
                    else:
                        assistant_content = f"Sorry, the model is unavailable and no fallback succeeded: {ce.message}"
                        break
                elif is_transient(ce):
                    # P1.1/Phase-1: transient error — bounded backoff retry
                    # (status-code classified, Retry-After honored) instead
                    # of failing the whole turn on a single 429/503.
                    async def _retry_llm_call():
                        prune_tool_results_only(llm_messages, model=get_model())
                        sanitize_messages(llm_messages)
                        return await _call_llm_with_tools(
                            llm_messages, tools, tool_choice=tool_choice,
                            endpoint=_eff_llm_v2.endpoint,
                            temperature=llm_overrides.get("temperature"),
                            max_tokens=llm_overrides.get("max_tokens"),
                        )
                    try:
                        llm_response = await call_with_transient_retry(_retry_llm_call)
                    except Exception as retry_err:
                        ce_retry = classify_api_error(retry_err)
                        logger.warning(
                            "Transient LLM error persisted (%s) in conversation %s: %s",
                            ce_retry.reason.value, conversation_id, ce_retry.message,
                        )
                        assistant_content = (
                            f"Sorry, the AI service is temporarily unavailable "
                            f"({ce_retry.message}). Please try again."
                        )
                        break
                else:
                    # Non-retryable error
                    assistant_content = f"Sorry, I encountered an error: {ce.message}"
                    break

            assistant_content = llm_response.get("content", "")
            raw_tool_calls = llm_response.get("tool_calls", [])

            # ── Clarification hard-stop (:::options) ──────────────────
            # An options block is a question to the user — the turn is DONE.
            # Suppress research tool calls in the same iteration and skip the
            # verification / nudge gates (weak LLMs ignore the prose rule and
            # research while waiting for the user).
            _opt_names = [tc.get("function", {}).get("name", "") for tc in raw_tool_calls]
            if _options_clarification(assistant_content, _opt_names):
                if raw_tool_calls:
                    logger.info(
                        "Suppressing %d tool call(s) after :::options clarification "
                        "block (conv=%s, iter=%d): %s",
                        len(raw_tool_calls), conversation_id, iteration, _opt_names,
                    )
                    raw_tool_calls = []
                break

            if not raw_tool_calls:
                # No tool calls — LLM gave a final text response.
                # Anti-hallucination guardrail: if the agent has bound data
                # sources and the question is data-related, the LLM should
                # have called ask_data_agent. If it didn't, retry with a
                # hard nudge (up to MAX_GUARDRAIL_RETRIES times).
                guardrail_result = _check_hallucination_guardrail(
                    user_content,
                    data_ctx_extras,
                    tool_calls_for_frontend,
                    iteration,
                    guardrail_retries,
                )
                if guardrail_result.action == "nudge":
                    # Inject the guardrail nudge message and retry the LLM call.
                    llm_messages.append({
                        "role": "assistant",
                        "content": assistant_content,
                    })
                    llm_messages.append({
                        "role": "user",
                        "content": guardrail_result.message,
                    })
                    guardrail_retries += 1
                    continue
                elif guardrail_result.action == "fallback":
                    # Retries exhausted — replace hallucinated content with
                    # the safe fallback message and break.
                    assistant_content = guardrail_result.message
                    break
                # Guardrail did not trigger (action == "none") — accept the response
                # P2.1: Verification-on-stop — if code was edited without
                # verification, nudge the agent to verify before finishing.
                _verify_nudge = build_verify_on_stop_nudge(
                    llm_messages, attempts=_verify_attempts
                )
                if _verify_nudge:
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _verify_nudge})
                    _verify_attempts += 1
                    continue
                # P2.1.5: PPTX turn-guard — if the user asked for a PPT deck
                # and the model ended its turn with text only (no tool call),
                # nudge it to call create_artifact(type="pptx") now (cap 1/turn).
                # Runs BEFORE the self-eval gate: a missing deliverable
                # outranks answer-quality issues on this boundary.
                _pptx_guard = pptx_turn_guard(
                    user_content,
                    tool_calls_for_frontend,
                    budget_remaining=MAX_TOOL_ITERATIONS - iteration,
                    attempts=_pptx_nudge_attempts,
                )
                if _pptx_guard.action == "nudge":
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _pptx_guard.message})
                    _pptx_nudge_attempts += 1
                    # Fix 1b: the LAST allowed nudge arms a one-shot force so
                    # the next iteration forces create_artifact (the model can
                    # no longer deflect in prose).
                    if _pptx_guard.force_next:
                        _pptx_force_next_iteration = True
                    logger.info(
                        "v2 stream: pptx turn-guard nudge injected (conv=%s, iter=%d, force_next=%s)",
                        conversation_id, iteration, _pptx_guard.force_next,
                    )
                    continue
                if _pptx_guard.action == "disclose":
                    logger.warning(
                        "v2 stream: pptx deliverable not generated; disclosure "
                        "appended (conv=%s, iter=%d)", conversation_id, iteration,
                    )
                    assistant_content = (assistant_content or "") + " " + _pptx_guard.message
                # P2.1.6: File-deliverable turn-guard (v2 loop) — mirrors pptx
                # guard for html/docx/pdf/xlsx/md.  Blocked when pptx already
                # nudged/forced this iteration.
                if _pptx_guard.action == "none":
                    _file_guard = file_turn_guard(
                        user_content,
                        tool_calls_for_frontend,
                        budget_remaining=MAX_TOOL_ITERATIONS - iteration,
                        attempts=_file_nudge_attempts,
                    )
                    if _file_guard.action == "nudge":
                        llm_messages.append({"role": "assistant", "content": assistant_content})
                        llm_messages.append({"role": "user", "content": _file_guard.message})
                        _file_nudge_attempts += 1
                        if _file_guard.force_next:
                            _file_force_next_iteration = True
                        logger.info(
                            "v2 stream: file turn-guard nudge injected "
                            "(conv=%s, iter=%d, format=%s, force_next=%s)",
                            conversation_id, iteration,
                            _file_guard.detected_format, _file_guard.force_next,
                        )
                        continue
                    if _file_guard.action == "disclose":
                        logger.warning(
                            "v2 stream: file deliverable not generated; disclosure "
                            "appended (conv=%s, iter=%d, format=%s)",
                            conversation_id, iteration, _file_guard.detected_format,
                        )
                        assistant_content = (assistant_content or "") + " " + _file_guard.message
                # P2.2: Universal Self-Evaluation & Re-Planning gate (re-plan
                # up to SELF_EVAL_MAX_REPLANS times before best-effort answer).
                _gate_result = await _check_answer_verification_gate(
                    user_content,
                    tool_calls_for_frontend,
                    assistant_content,
                    attempts=_gate_attempts,
                    budget_remaining=MAX_TOOL_ITERATIONS - iteration,
                    catalog_meta=(data_ctx_extras or {}).get("catalog_meta"),
                )
                if _gate_result.action == "nudge":
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _gate_result.message})
                    _gate_attempts += 1
                    continue
                if _gate_result.action == "disclose":
                    assistant_content = (assistant_content or "") + _gate_result.message
                break

            # Parse all tool calls first so we can execute them in parallel
            parsed_calls = []
            for tc in raw_tool_calls:
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                args_str = func.get("arguments", "{}")
                tool_call_id = tc.get("id", str(uuid.uuid4()))
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {}
                parsed_calls.append({
                    "tool_name": tool_name,
                    "args": args,
                    "args_str": args_str,
                    "tool_call_id": tool_call_id,
                })

            # ── Dashboard guard interception ──────────────────────────
            # Same logic as the v3 stream: if the guard fired but the LLM
            # ignored tool_choice and returned execute_query, block it and
            # inject a synthetic tool result forcing a create_dashboard retry.
            if parsed_calls:
                _dash_guard_names = {p["tool_name"] for p in parsed_calls}
                _v2_build_tool = dashboard_build_tool()
                # ── Data-contract confirmation gate (T7) ──────────────
                # Same gate as the v3 stream: never let the build tool run on
                # an unconfirmed data contract (ambiguous request, no schema
                # grounding, no user approval). Block + inject clarification.
                if _v2_build_tool in _dash_guard_names and contract_confirmation_needed(
                    user_content, tool_calls_for_frontend
                ):
                    _v2_build_call = next(
                        (p for p in parsed_calls if p["tool_name"] == _v2_build_tool),
                        None,
                    )
                    _v2_non_build = [p for p in parsed_calls if p["tool_name"] != _v2_build_tool]
                    if _v2_build_call is not None:
                        logger.warning(
                            "v2 stream: data-contract gate blocked %s on unconfirmed "
                            "contract (conv=%s, iter=%d).",
                            _v2_build_tool, conversation_id, iteration,
                        )
                        llm_messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": _v2_build_call["tool_call_id"],
                                "type": "function",
                                "function": {
                                    "name": _v2_build_tool,
                                    "arguments": _v2_build_call["args_str"],
                                },
                            }],
                        })
                        llm_messages.append({
                            "role": "tool",
                            "tool_call_id": _v2_build_call["tool_call_id"],
                            "content": (
                                "BLOCKED by the data-contract guard: you tried to build a "
                                "live dashboard before confirming the data contract. You "
                                "MUST first inspect the real schema with `describe_schema` "
                                "or `inspect_data_source`. If the user's request is "
                                "ambiguous about which table, metric, or aggregation to "
                                "use, ask the user ONE clarifying question and wait for "
                                "the answer. NEVER invent table or column names — a "
                                "dashboard built on fabricated data is worse than none."
                            ),
                        })
                        tool_calls_for_frontend.append({
                            "id": f"contract_gate_{uuid.uuid4()}",
                            "name": "data_contract_gate",
                            "status": "blocked",
                            "results": {
                                "blocked": _v2_build_tool,
                                "reason": "data contract not confirmed (ambiguous request, no schema grounding)",
                            },
                        })
                        parsed_calls = _v2_non_build
                        if not _v2_non_build:
                            continue
                if dashboard_guard_should_block_queries(
                    _dash_guard_names, _v2_build_tool, dashboard_forced,
                ):
                    _blocked_names = [
                        p["tool_name"]
                        for p in parsed_calls
                        if p["tool_name"] in dashboard_guard_blocked_tools()
                    ]
                    logger.warning(
                        "v2 stream: dashboard guard intercepted %s after "
                        "schema/design pass (conv=%s, iter=%d).",
                        _blocked_names, conversation_id, iteration,
                    )
                    _blocked = [p for p in parsed_calls if p["tool_name"] in dashboard_guard_blocked_tools()]
                    _allowed = [p for p in parsed_calls if p["tool_name"] not in dashboard_guard_blocked_tools()]
                    _blocked_summary = ", ".join(sorted({p["tool_name"] for p in _blocked}))
                    for _blk in _blocked:
                        llm_messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": _blk["tool_call_id"],
                                "type": "function",
                                "function": {
                                    "name": _blk["tool_name"],
                                    "arguments": _blk["args_str"],
                                },
                            }],
                        })
                        llm_messages.append({
                            "role": "tool",
                            "tool_call_id": _blk["tool_call_id"],
                            "content": (
                                "BLOCKED by dashboard guard: the user asked "
                                "for a live dashboard. Schema + design pass "
                                "is done. STOP more "
                                f"{_blk['tool_name']} calls. Call "
                                "create_dashboard NOW with your widgets."
                            ),
                        })
                    if not _allowed:
                        tool_calls_for_frontend.append({
                            "id": f"guard_blocked_{uuid.uuid4()}",
                            "name": "dashboard_guard_intercept",
                            "status": "blocked",
                            "results": {
                                "blocked": _blocked_summary,
                                "reason": "guard: schema/design pass done; only create_dashboard allowed",
                            },
                        })
                        continue
                    parsed_calls = _allowed

            # Intercept path (R5): if the LLM wants to call `create_agent`
            # in this batch, intercept the call BEFORE execution, use its
            # arguments as the draft payload, and pause for the Decision
            # Summary card. Any other tool calls in the same batch (e.g.
            # `list_tools`) still execute normally; the create_agent entry
            # is recorded as `awaiting_decision_summary` in the tool_calls
            # list so the chat bubble shows the partial state.
            intercepted, intercept_payload, intercept_index = _intercept_create_agent(parsed_calls)
            if intercepted:
                logger.info(
                    "create_agent intercept (v2 main): conv=%s tool_call_id=%s "
                    "payload_keys=%s siblings=%d",
                    conversation_id,
                    parsed_calls[intercept_index]["tool_call_id"],
                    sorted((intercept_payload or {}).keys()),
                    len(parsed_calls) - 1,
                )
                # Execute the other calls first (if any) so the conversation
                # state stays consistent — the LLM's tool results feed back
                # into the next iteration if the user clicks Cancel.
                sibling_calls = [c for i, c in enumerate(parsed_calls) if i != intercept_index]
                ctx_for_siblings = {
                    "conversation_id": conversation_id,
                    "agent_app_id": agent_app_id,
                    "agent_name": agent_name,
                    "conversation_metadata": conv.metadata_ or {},
                    "chat_session_id": chat_session_id,
                    **(data_ctx_extras or {}),
                }
                sibling_results: list[dict] = []
                if sibling_calls:
                    if len(sibling_calls) == 1:
                        sibling_results = [await execute_tool(
                            sibling_calls[0]["tool_name"],
                            sibling_calls[0]["args"],
                            db,
                            user.id if user else None,
                            context=ctx_for_siblings,
                        )]
                    else:
                        async def _exec_v2_sibling(call):
                            return await execute_tool(
                                call["tool_name"], call["args"], db,
                                user.id if user else None,
                                context=ctx_for_siblings,
                            )
                        raw_sib = await asyncio.gather(
                            *[_exec_v2_sibling(c) for c in sibling_calls],
                            return_exceptions=True,
                        )
                        for i, r in enumerate(raw_sib):
                            if isinstance(r, Exception):
                                logger.warning("sibling tool '%s' raised: %s", sibling_calls[i]["tool_name"], r)
                                sibling_results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
                            else:
                                sibling_results.append(r)
                # Record sibling tool calls in the assistant message.
                for sib_call, sib_result in zip(sibling_calls, sibling_results):
                    sib_name = sib_call["tool_name"]
                    sib_display = TOOL_DISPLAY_NAMES.get(sib_name, sib_name)
                    sib_record = {
                        "id": sib_call["tool_call_id"],
                        "name": sib_display,
                        "arguments_string": sib_call["args_str"],
                        "results": sib_result,
                        "status": "completed" if isinstance(sib_result, dict) and sib_result.get("success") else "failed",
                    }
                    if sib_name in INTERNAL_TOOLS:
                        sib_record["display_projection"] = _internal_tool_projection(sib_name)
                    tool_calls_for_frontend.append(sib_record)
                    llm_messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": sib_call["tool_call_id"], "type": "function",
                                        "function": {"name": sib_name, "arguments": sib_call["args_str"]}}],
                    })
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": sib_call["tool_call_id"],
                        "content": json.dumps(sib_result),
                    })
                # ReAct reflexion: if any sibling tool failed, inject a
                # critique system message before the next LLM iteration.
                _inject_reflexion_critique(llm_messages, sibling_calls, sibling_results)
                # Now record the create_agent entry as `awaiting_decision_summary`
                # so the chat bubble shows the partial state.
                intercepted_call = parsed_calls[intercept_index]
                intercepted_record = {
                    "id": intercepted_call["tool_call_id"],
                    "name": TOOL_DISPLAY_NAMES.get("create_agent", "create_agent"),
                    "arguments_string": intercepted_call["args_str"],
                    "status": "awaiting_decision_summary",
                }
                tool_calls_for_frontend.append(intercepted_record)
                # Persist pause + return.
                paused, _stripped, _note = _persist_decision_summary_pause(
                    db, conv, messages, assistant_msg_id,
                    tool_calls_for_frontend, assistant_content,
                    tool_call_payload=intercept_payload,
                )
                if paused:
                    try:
                        db.refresh(conv)
                    except Exception:
                        pass
                    return conv.to_dict()
                # Sanitiser rejected (e.g. no name) — fall through and let the
                # normal loop continue. Should be rare.

            # Build shared context for all tool calls
            ctx = {
                "conversation_id": conversation_id,
                "agent_app_id": agent_app_id,
                "agent_name": agent_name,
                "conversation_metadata": conv.metadata_ or {},
                "chat_session_id": chat_session_id,
                **(data_ctx_extras or {}),
                "endpoint": _eff_llm_v2.endpoint,
            }

            # Execute tools: sequential for single call, parallel for multiple.
            # return_exceptions=True ensures one failure doesn't cancel siblings
            # (which would leave un-replied tool_use blocks that the LLM API rejects).
            # P0: guardrail before_call checks for loop patterns before executing.
            # P2-12: shared core in app.services.agent_loop.tool_executor.
            async def _invoke(tool_name, args):
                return await execute_tool(
                    tool_name, args, db, user.id if user else None, context=ctx,
                )

            results = await execute_tool_batch(
                parsed_calls,
                before_call=guard_ctrl.before_call,
                invoke=_invoke,
                blocked_result_factory=_guardrail_synthetic_result,
            )

            # Process results in original order, building frontend records + LLM messages.
            # If any result requires approval, pause the loop after recording the
            # awaiting_approval tool call — the user must approve/reject before
            # execution continues via the /resume endpoint.
            paused_for_approval = False
            pending_tool = None
            for call, result in zip(parsed_calls, results):
                tool_name = call["tool_name"]
                args_str = call["args_str"]
                tool_call_id = call["tool_call_id"]
                display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)

                if isinstance(result, dict) and result.get("requires_approval"):
                    # Pause: create an awaiting_approval tool call record and
                    # store resume state so the /resume endpoint can continue.
                    tool_call_record = {
                        "id": tool_call_id,
                        "name": display_name,
                        "arguments_string": args_str,
                        "results": result,
                        "status": "awaiting_approval",
                        "approval_id": result.get("approval_id"),
                        "reason": result.get("reason", ""),
                    }
                    tool_calls_for_frontend.append(tool_call_record)
                    pending_tool = {
                        "tool_name": tool_name,
                        "args": call["args"],
                        "args_str": args_str,
                        "tool_call_id": tool_call_id,
                        "approval_id": result.get("approval_id"),
                        # Capture the remaining calls in this batch that haven't
                        # been processed yet (they'll execute after resume).
                        "remaining_calls": [
                            c for c in parsed_calls[parsed_calls.index(call) + 1:]
                        ],
                    }
                    paused_for_approval = True
                    break

                # ── Report synthesis: enrich ask_data_agent results ──────
                # When the data agent returns rows, force a second LLM turn
                # to synthesize a rich ReportCardPayload (KPIs, chart,
                # insights, actions). This runs in-line so the frontend gets
                # the enriched payload on the same tool_call record.
                if tool_name == "ask_data_agent" and isinstance(result, dict) and result.get("rows"):
                    try:
                        _synth_endpoint = _eff_llm_v2.endpoint

                        async def _synth_call(system_prompt, msgs, _ep=_synth_endpoint):
                            return await _call_synthesis_llm(
                                system_prompt, msgs, endpoint=_ep
                            )

                        # FIX 2026-08-23: resolve skill context for synthesis
                        _v2_skill_name, _v2_skill_method = _resolve_skill_for_synthesis(
                            tool_calls_for_frontend,
                            selected_skill_v2,
                            selected_skill_id_v2,
                            db,
                        )
                        synth_result = await synthesize_report(
                            user_message=user_content,
                            rows=result.get("rows"),
                            sql=result.get("sql"),
                            source_name=result.get("source_name"),
                            source_id=result.get("source_id"),
                            call_llm_fn=_synth_call,
                            skill_name=_v2_skill_name,
                            skill_methodology=_v2_skill_method,
                        )
                        if synth_result.report_card_payload is not None:
                            # Always attach payload + synthesis text so the
                            # frontend can suppress DataTableCard and show
                            # the narrative.
                            result["report_card_payload"] = synth_result.report_card_payload.model_dump()
                            result["synthesis_text"] = synth_result.assistant_content
                            # ── FINALIZE: persist Artifact row + HTML blob ──
                            # FIX 2026-08-22: only create an artifact card
                            # when the user explicitly asked for a file
                            # deliverable. For simple data questions the
                            # synthesis text IS the answer — no card needed.
                            if _orch_doc_format:
                                artifact, file_exports = finalize_into_artifact(
                                    db,
                                    conversation_id=conversation_id,
                                    agent_name=agent_name,
                                    user_message=user_content,
                                    source=result.get("source_name"),
                                    sql=result.get("sql"),
                                    payload=synth_result.report_card_payload,
                                    message_id=assistant_msg_id,
                                )

                                if file_exports:
                                    result["file_exports"] = file_exports
                                    primary_fmt = next(iter(file_exports))
                                    export = file_exports[primary_fmt]
                                    export_artifact_id = export.get("artifact_id")
                                    if export_artifact_id:
                                        result["artifact_id"] = export_artifact_id
                                        artifact_ids.append(export_artifact_id)
                                if artifact is not None:
                                    if not file_exports:
                                        result["artifact_id"] = artifact.id
                                        artifact_ids.append(artifact.id)
                                logger.info(
                                    "FINALIZE: artifact_id=%s attached to tool result for conv=%s",
                                    getattr(artifact, 'id', None) if artifact else None, conversation_id,
                                )
                            else:
                                artifact = None
                                file_exports = {}
                                logger.info(
                                    "FINALIZE: skipped artifact creation (no file intent) for conv=%s",
                                    conversation_id,
                                )

                            logger.info(
                                "Synthesized report card for conv=%s (title=%r, kpis=%d, insights=%d)",
                                conversation_id,
                                synth_result.report_card_payload.title,
                                len(synth_result.report_card_payload.kpis),
                                len(synth_result.report_card_payload.insights),
                            )
                    except Exception as synth_err:
                        logger.warning(
                            "Report synthesis failed (non-fatal) for conv=%s: %s",
                            conversation_id, synth_err,
                        )
                elif tool_name == "ask_data_agent" and _should_finalize_no_data(result, _orch_doc_format):
                    # ── Empty rows + file-format intent ──────────────────
                    # The query ran but returned 0 rows. Without this
                    # branch the report/artifact chain dies here (the rich
                    # gate above requires truthy rows). Finalize a graceful
                    # "no data" report deterministically so the user still
                    # receives the requested file with a clear narrative.
                    #
                    # FIX 2026-08-24: if a previous turn successfully fetched
                    # data, skip the no-data artifact here and let the post-loop
                    # deferred finalize reuse the historical rows instead of
                    # producing a useless warning-filled file.
                    _has_historical_data = False
                    try:
                        from app.services.generation_orchestrator import (
                            _mine_historical_answer_rows,
                        )
                        _has_historical_data = bool(
                            _mine_historical_answer_rows(messages)
                        )
                    except Exception:
                        pass
                    if _has_historical_data:
                        logger.info(
                            "MID-LOOP no-data skipped: historical data exists "
                            "from previous turn (conv=%s)",
                            conversation_id,
                        )
                        # Mark the result so the frontend shows a pending state
                        # rather than a completed no-data card.
                        result["awaiting_historical_reuse"] = True
                    else:
                        try:
                            no_data_payload = build_no_data_payload(
                                user_message=user_content,
                                source=result.get("source_name"),
                                sql=result.get("sql"),
                            )
                            artifact, file_exports = finalize_into_artifact(
                                db,
                                conversation_id=conversation_id,
                                agent_name=agent_name,
                                user_message=user_content,
                                source=result.get("source_name"),
                                sql=result.get("sql"),
                                payload=no_data_payload,
                                message_id=assistant_msg_id,
                            )
                            result["report_card_payload"] = no_data_payload.model_dump()
                            result["no_data"] = True

                            if file_exports:
                                result["file_exports"] = file_exports
                                primary_fmt = next(iter(file_exports))
                                export_artifact_id = file_exports[primary_fmt].get("artifact_id")
                                if export_artifact_id:
                                    result["artifact_id"] = export_artifact_id
                                    artifact_ids.append(export_artifact_id)
                            if artifact is not None and not file_exports:
                                result["artifact_id"] = artifact.id
                                artifact_ids.append(artifact.id)
                            logger.info(
                                "FINALIZE (no-data): artifact_id=%s attached for conv=%s (doc_format=%s)",
                                getattr(artifact, 'id', None) if artifact else None,
                                conversation_id, _orch_doc_format,
                            )
                        except Exception as no_data_err:
                            logger.warning(
                                "No-data finalize failed (non-fatal) for conv=%s: %s",
                                conversation_id, no_data_err,
                            )

                tool_call_record = {
                    "id": tool_call_id,
                    "name": display_name,
                    "arguments_string": args_str,
                    "results": result,
                    "status": "completed" if result.get("success") else "failed",
                }
                if tool_name in INTERNAL_TOOLS:
                    tool_call_record["display_projection"] = _internal_tool_projection(tool_name)
                tool_calls_for_frontend.append(tool_call_record)

                # Gap B checkpoint: the moment a report card or artifact
                # exists, persist it — a crash or dropped connection later
                # in the turn must not erase finished work.
                if isinstance(result, dict) and (
                    result.get("report_card_payload") or result.get("artifact_id")
                ):
                    _checkpoint_partial_assistant_msg(
                        db, conv, messages, assistant_msg_id,
                        tool_calls_for_frontend, artifact_ids,
                    )

                llm_messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": args_str,
                            },
                        }
                    ],
                })
                # P0: apply Layer 2 (per-result) persistence to large results
                _result_str = _persisted_result_str(
                    tool_name, result, conversation_id,
                    context_window_tokens=(
                        _eff_llm_v2.endpoint.context_window
                        if _eff_llm_v2.endpoint else None
                    ),
                )
                llm_messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": _result_str,
                })
                # P0: guardrail after_call records outcome for loop detection
                _gd_after = guard_ctrl.after_call(tool_name, call["args"], _result_str)
                # P8: Record learning for cross-session technique tracking
                try:
                    from app.services.learning_graph import record_learning as _rl
                    _rl(agent_app_id, f"called {tool_name}",
                        "success" if isinstance(result, dict) and result.get("success") else "failure",
                        context=(user_content or "")[:200], tool=tool_name)
                except Exception:
                    pass

            # P0: Layer 3 — apply per-turn aggregate budget to this batch's results
            if not paused_for_approval:
                _batch_ids = [c["tool_call_id"] for c in parsed_calls]
                _batch_names = [c["tool_name"] for c in parsed_calls]
                _apply_turn_budget_to_messages(
                    llm_messages, _batch_ids, _batch_names, conversation_id,
                    context_window_tokens=(
                        _eff_llm_v2.endpoint.context_window
                        if _eff_llm_v2.endpoint else None
                    ),
                )

            # P0: if guardrail controller tripped a halt, inject nudge and break
            if not paused_for_approval and guard_ctrl.halt_decision:
                _hd = guard_ctrl.halt_decision
                logger.warning(
                    "Guardrail halt in conversation %s: %s (tool=%s, count=%d)",
                    conversation_id, _hd.code, _hd.tool_name, _hd.count,
                )
                metrics.record_guardrail_halt(_hd.code)
                llm_messages.append({
                    "role": "user",
                    "content": (
                        f"A tool loop was detected: {_hd.message} "
                        "Use the results you already have and produce your final answer."
                    ),
                })
                break

            # P0: refund iteration for execute_code turns (programmatic tool-calling
            # doesn't count against the budget -- the code itself makes tool calls)
            if not paused_for_approval and all(c["tool_name"] == "execute_code" for c in parsed_calls):
                if all(isinstance(r, dict) and r.get("success") is True for r in results):
                    conv_budget.refund()

            # ReAct reflexion: if any tool in this batch failed, inject a
            # critique system message so the next iteration reasons about
            # the failure instead of blindly retrying.  Guarded by
            # ``not paused_for_approval`` because an approval break leaves
            # un-appended results in ``results`` that must not be critiqued.
            if not paused_for_approval:
                _inject_reflexion_critique(llm_messages, parsed_calls, results)

            if paused_for_approval:
                # Persist resume state so the /resume endpoint can continue.
                # Store everything needed to resume the LLM tool-calling loop.
                conv.metadata_ = conv.metadata_ or {}
                conv.metadata_["_resume_state"] = {
                    "llm_messages": llm_messages,
                    "iteration": iteration,
                    "tool_calls_for_frontend": tool_calls_for_frontend,
                    "agent_name": agent_name,
                    "agent_app_id": agent_app_id,
                    "data_ctx_extras": data_ctx_extras,
                    "user_content": user_content,
                    "guardrail_retries": guardrail_retries,
                    "system_prompt": system_prompt,
                    "tools": tools,
                    "pending_tool": pending_tool,
                }
                conv.status = "awaiting_approval"
                # Build a partial assistant message so the frontend can render
                # the tool calls that are in progress.
                assistant_msg = {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": "",
                    "created_date": datetime.now(timezone.utc).isoformat(),
                    "tool_calls": tool_calls_for_frontend,
                }
                messages.append(assistant_msg)
                conv.messages = messages
                conv.updated_date = datetime.now(timezone.utc)
                db.commit()
                db.refresh(conv)
                logger.info(
                    "Conversation %s paused for approval (approval_id=%s, tool=%s)",
                    conversation_id, pending_tool["approval_id"], pending_tool["tool_name"],
                )
                return conv.to_dict()

        # Decision-summary pause (R4): if the assistant text contains a
        # `:::decision-summary` block, persist the pending payload to
        # `conv.metadata_` and short-circuit the normal save path. The
        # _persist_decision_summary_pause helper writes both the
        # assistant message and the conversation metadata, so we
        # ``return`` immediately and skip the standard
        # `conv.messages = messages;` flow below.
        paused, _stripped, _note = _persist_decision_summary_pause(
            db, conv, messages, assistant_msg_id,
            tool_calls_for_frontend, assistant_content,
        )
        if paused:
            # Fresh refresh so the response includes the metadata
            try:
                db.refresh(conv)
            except Exception:
                pass
            return conv.to_dict()

        # Force-pause (R6): after 2+ iterations of exploration without a
        # `create_agent` call (and without a fence), build a decision
        # summary from the user message + sensible defaults. Breaks the
        # discovery loop deterministically when the LLM keeps
        # list_tools/skills-ing instead of building.
        #
        # IMPORTANT: trigger on the accumulated tool-call COUNT, not
        # the iteration counter. LLMs that support parallel tool calls
        # (DeepSeek, GPT-4, Claude) issue ALL discovery calls in a
        # single iteration, so the iteration counter never reaches 2.
        # `len(tool_calls_for_frontend) >= 2` is iteration-agnostic —
        # it fires for both parallel and sequential tool calls. See
        # tests/test_force_pause_parallel_tools.py for the regression
        # test.
        if (
            len(tool_calls_for_frontend) >= 2
            and _user_wants_save_directly(user_content)
        ):
            forced = _build_forced_decision_summary(user_content)
            forced_clean = _sanitize_decision_payload(forced)
            if forced_clean.get("name"):
                logger.info(
                    "force-pause (v2 main): conv=%s iteration=%d "
                    "user_cue='save directly' forced_payload_keys=%s",
                    conversation_id, iteration,
                    sorted(forced_clean.keys()),
                )
                synthetic_id = f"forced_{uuid.uuid4()}"
                tool_calls_for_frontend.append({
                    "id": synthetic_id,
                    "name": TOOL_DISPLAY_NAMES.get("create_agent", "create_agent"),
                    "arguments_string": json.dumps(forced_clean),
                    "status": "awaiting_decision_summary",
                    "forced": True,
                })
                assistant_content = (
                    (assistant_content or "")
                    + "\n\nI have enough to build the agent. I'm pausing "
                      "here so you can review before I save it."
                ).strip()
                paused, _stripped, _note = _persist_decision_summary_pause(
                    db, conv, messages, assistant_msg_id,
                    tool_calls_for_frontend, assistant_content,
                    tool_call_payload=forced_clean,
                )
                if paused:
                    try:
                        db.refresh(conv)
                    except Exception:
                        pass
                    return conv.to_dict()

        # Build the final assistant message with tool_calls
        assistant_msg = {
            "id": assistant_msg_id,
            "role": "assistant",
            "content": assistant_content,
            "created_date": datetime.now(timezone.utc).isoformat(),
        }
        if tool_calls_for_frontend:
            assistant_msg["tool_calls"] = tool_calls_for_frontend
        # Attach artifact_ids so the frontend can render ArtifactPreviewCard
        if artifact_ids:
            assistant_msg["artifact_ids"] = artifact_ids
        # Surface create_artifact results as artifacts
        _artifacts = _collect_artifact_results(
            tool_calls_for_frontend, assistant_msg_id, conversation_id, db,
        )
        if _artifacts:
            assistant_msg["artifacts"] = _artifacts
        # Derive and attach the execution trace (Reasoning & actions)
        assistant_msg["trace"] = _derive_trace_from_response(
            assistant_content, tool_calls_for_frontend,
        )

        # ── Marker contract: ◤MD_DOCX◤ / ◤HTML_DOCX◤ / ◤PPTX◤ → create_artifact ──
        # Skills like `docx`, `pptx`, and `artifacts-builder` instruct the
        # LLM to emit a marker at the end of its reply describing the file it
        # just wrote to `outputs/`. Route each marker through the generation
        # orchestrator (which properly *awaits* the async _create_artifact_tool
        # — the previous synchronous call produced a never-awaited coroutine,
        # so markers never yielded artifacts), then strip markers from the
        # user-visible text. Best-effort: never break the chat response.
        # (_orch_doc_format was computed before the tool loop.)
        try:
            from app.services.generation_orchestrator import (
                ensure_artifact_for_doc_request,
                fulfill_markers,
            )

            assistant_content, _created = await fulfill_markers(
                assistant_content,
                db=db,
                context={
                    "conversation_id": conv.id,
                    "agent_app_id": agent_app_id,
                },
            )
            # Also post-process the LLM's own [[RESULT]]...[[END]] blocks.
            # Some system prompts (and most LLM hallucinations) tell the
            # model to emit a [[RESULT]] block with a *fake* artifact id;
            # without this step the frontend renders a card pointing at a
            # non-existent artifact and the user sees a 404.  Fulfilling
            # here creates a real artifact and rewrites the id in the
            # assistant text so the resource card works end-to-end.
            try:
                from app.services.result_block_processor import (
                    fulfill_result_blocks,
                )

                assistant_content, _result_created = await fulfill_result_blocks(
                    assistant_content,
                    db=db,
                    context={
                        "conversation_id": conv.id,
                        "agent_app_id": agent_app_id,
                    },
                )
                if _result_created:
                    _created = list(_created) + _result_created
            except Exception as _rb_err:
                logger.warning(
                    "add_message: result_block post-processor raised (non-fatal): %s",
                    _rb_err,
                )

            # ── Hybrid guardrail: LLM-driven detection with regex fallback ──
            # Per the user's explicit feedback: keyword patterns are
            # brittle, the LLM should be the brain.  This guardrail
            # uses the LLM-based intent-classifier + self-critic as the
            # primary path and falls back to the keyword patterns in
            # :mod:`app.services.turn_action` when the LLM is unavailable
            # or returns ``unclassified``.  Never raises.
            try:
                from app.services.hybrid_guardrail import (
                    detect_and_correct_refusal,
                )

                async def _hg_call_llm(q, results):
                    """Re-ask the LLM with the search results as context."""
                    bullets = "\n".join(
                        f"- [{r.get('title','?')}]({r.get('url','?')}): {r.get('snippet','')}"
                        for r in results
                    )
                    followup_msgs = list(messages) + [
                        {
                            # 2026-08-25: role=user not role=system — vLLM rejects
                            # mid-list system messages with HTTP 400.
                            "role": "user",
                            "content": (
                                "You DO have access to web_search and web_extract. "
                                "Use these live search results to answer the user's "
                                "request. Cite the source links. If results are "
                                "insufficient, run another web_search."
                            ),
                        },
                        {
                            "role": "user",
                            "content": (
                                f"Here are live web search results for "
                                f"'{q}':\n{bullets}\n\n"
                                f"Please answer my original request using these results."
                            ),
                        },
                    ]
                    try:
                        out = await _call_llm_with_tools(
                            followup_msgs,
                            tools=tools,
                            tool_choice="auto",
                            temperature=temperature,
                            max_tokens=max_tokens,
                        )
                    except Exception:
                        return None
                    return out.get("content") or None

                # Wrap the existing LLM call into a small classifier
                # hook so the hybrid guardrail can use it.
                async def _hg_llm_call(messages, **kwargs):
                    out = await _call_llm_with_tools(
                        messages,
                        tools=None,
                        tool_choice="none",
                        temperature=kwargs.get("temperature", 0.0),
                        max_tokens=kwargs.get("max_tokens", 200),
                    )
                    return out.get("content") or ""

                _hg_outcome = await detect_and_correct_refusal(
                    user_message=user_content,
                    assistant_text=assistant_content,
                    llm_call=_hg_llm_call,
                    call_llm=_hg_call_llm,
                    session_id=str(conv.id),
                    db=db,
                )
                if _hg_outcome.refused and _hg_outcome.followup_text:
                    assistant_content = _hg_outcome.followup_text
                    logger.info(
                        "add_message: hybrid guardrail fired (refusal_source=%s, "
                        "query=%r, %d results)",
                        _hg_outcome.refusal_source,
                        _hg_outcome.corrective_args.get("query"),
                        len(_hg_outcome.search_results),
                    )
            except Exception as _hg_err:
                logger.warning(
                    "add_message: hybrid guardrail raised (non-fatal): %s",
                    _hg_err,
                )
            # Server-driven fallback (Q1): the user asked for a file but the
            # LLM produced neither a marker nor a create_artifact tool call.
            # Synthesize a minimal payload from the prose so the user always
            # receives a downloadable artifact.
            _fallback = await ensure_artifact_for_doc_request(
                doc_format=_orch_doc_format,
                assistant_content=assistant_content,
                already_created=_created,
                tool_calls_for_frontend=tool_calls_for_frontend,
                artifact_ids=artifact_ids,
                db=db,
                context={
                    "conversation_id": conv.id,
                    "agent_app_id": agent_app_id,
                    "user_message": user_content,
                },
            )
            if _fallback:
                _created = list(_created) + [_fallback]
            # Surface orchestrator-created artifacts on the assistant message
            # and register synthetic create_artifact tool_call records so
            # artifact_ids/linking stay consistent with the LLM-driven path.
            # link_to_message and _collect_artifact_results are idempotent
            # (per artifact_id), so a full re-collect over the extended list
            # cannot duplicate message_artifacts rows or artifact entries.
            if _created:
                for _art in _created:
                    tool_calls_for_frontend.append({
                        "id": f"orch-{_art.get('artifact_id')}",
                        "name": "create_artifact",
                        "status": "completed",
                        "results": _art,
                    })
                    if _art.get("artifact_id") and _art["artifact_id"] not in artifact_ids:
                        artifact_ids.append(_art["artifact_id"])
                assistant_msg["tool_calls"] = tool_calls_for_frontend
                assistant_msg["artifact_ids"] = artifact_ids
                _all_artifacts = _collect_artifact_results(
                    tool_calls_for_frontend, assistant_msg_id, conversation_id, db,
                )
                if _all_artifacts:
                    assistant_msg["artifacts"] = _all_artifacts
            assistant_msg["content"] = assistant_content
            # Re-derive trace (it references the content)
            assistant_msg["trace"] = _derive_trace_from_response(
                assistant_content, tool_calls_for_frontend,
            )
        except Exception as _marker_outer_err:
            logger.warning(
                "Marker parsing block failed (non-fatal): %s",
                _marker_outer_err,
            )

        messages.append(assistant_msg)

    conv.messages = messages
    conv.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)

    # QUALITY_EVAL (Part 2 — Gap Analysis): run standalone quality
    # evaluation in the ReAct (v2) path.  Only fires when
    # QUALITY_EVAL_ALL_PATHS is True.  Non-fatal: any failure or
    # heuristic-only result leaves assistant_content unchanged.
    try:
        if getattr(settings, "QUALITY_EVAL_ALL_PATHS", True):
            from app.services.synexia.quality_eval import evaluate_response_quality
            _qe = evaluate_response_quality(
                user_message=user_content or prompt,
                assistant_text=assistant_content,
                response_prompt=system_prompt or "",
            )
            if _qe.final_text and _qe.final_text != assistant_content:
                assistant_content = _qe.final_text
                # Update the persisted message content
                assistant_msg["content"] = assistant_content
                conv.messages = messages  # messages already has assistant_msg appended
                conv.updated_date = datetime.now(timezone.utc)
                db.commit()
                db.refresh(conv)
    except Exception as _qe_err:
        logger.warning("ReAct quality eval failed (non-fatal): %s", _qe_err)

    # Fire-and-forget: extract memories from this conversation.
    # Only triggers when there are enough messages (≥4, checked internally).
    # Uses an independent DB session so it doesn't block or conflict.
    if agent_app_id and len(messages) >= 4:
        asyncio.create_task(_bg_extract_memories(
            agent_app_id, list(messages), user.id if user else None,
            project_id=getattr(conv, "project_id", None),
        ))

    # P2.3: Background self-improvement review — every N turns, spawn a
    # fire-and-forget review that asks the LLM "should anything be saved to
    # memory from this conversation?". Uses the memory tool only.
    try:
        _msg_count = len(messages)
        _review_threshold = DEFAULT_REVIEW_INTERVAL * 4  # ~5 turns (4 msgs/turn w/ tools)
        if _msg_count >= _review_threshold and _msg_count % _review_threshold < 4:
            spawn_background_review(
                conversation_id,
                list(messages),
                model=getattr(settings, "LLM_MODEL", None),
                api_key=getattr(settings, "LLM_API_KEY", None),
                base_url=getattr(settings, "LLM_BASE_URL", None),
            )
    except Exception:
        pass  # never let background review break the response

    return conv.to_dict()


@router.post("/apps/{app_id}/agents/conversations/{conversation_id}/resume")
async def resume_conversation(
    app_id: str,
    conversation_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Resume a conversation that was paused for approval.

    Called by the frontend after the user approves or rejects an ApprovalRequest
    via the governance API. Restores the saved LLM loop state, executes or skips
    the pending tool, then continues the tool-calling loop from where it paused.
    """
    conv = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.is_deleted == False,
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.created_by_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to access this conversation")

    # Wire the authenticated user's role into the request-scoped trace
    # context so RBAC tool-filtering in tool_registry.get_schemas() (which
    # reads TraceContext.current_role()) activates for this request.
    TraceContext.set(
        session_id=str(conv.id),
        user_id=str(user.id),
        agent_name=conv.agent_name,
        role=user.role,
    )

    # ── User-level LLM overrides ─────────────────────────────────────
    llm_overrides = get_user_llm_overrides(db, user.id)

    # ── Resolve project-bound LLM endpoint (same precedence as add_message) ──
    _resume_eff_llm = resolve_effective_llm(
        db,
        project_id=getattr(conv, "project_id", None),
        agent_name=conv.agent_name,
        user_model=None,
        user_is_admin=(user.role == "admin"),
        org_id=conv.org_id,
        app_id=conv.app_id,
    )

    resume_state = (conv.metadata_ or {}).get("_resume_state")
    if not resume_state:
        raise HTTPException(status_code=400, detail="No pending approval to resume")

    pending_tool = resume_state.get("pending_tool")
    if not pending_tool:
        raise HTTPException(status_code=400, detail="No pending tool in resume state")

    # Check the approval status from the ApprovalRequest record
    approval_id = pending_tool.get("approval_id")
    approval_status = None
    if approval_id:
        try:
            from app.services.governance.approval_service import ApprovalService
            approval_svc = ApprovalService(db)
            approval_req = approval_svc.get_request(approval_id)
            if approval_req:
                approval_status = approval_req.status
        except Exception as e:
            logger.warning("Failed to check approval status for %s: %s", approval_id, e)

    # Restore LLM loop state
    llm_messages = resume_state.get("llm_messages", [])
    iteration = resume_state.get("iteration", 0)
    tool_calls_for_frontend = resume_state.get("tool_calls_for_frontend", [])
    agent_name = resume_state.get("agent_name")
    agent_app_id = resume_state.get("agent_app_id")
    data_ctx_extras = resume_state.get("data_ctx_extras", {})
    user_content = resume_state.get("user_content", "")
    guardrail_retries = resume_state.get("guardrail_retries", 0)
    tools = resume_state.get("tools")

    # Resolve the chat session that owns this conversation so the resumed
    # tool call (often a write) can link records to the chat session.
    chat_session_id: str | None = None
    try:
        from app.models.chat_session import ChatSession
        sess_row = db.query(ChatSession).filter(
            ChatSession.conversation_id == conv.id,
            ChatSession.is_deleted == False,  # noqa: E712
        ).order_by(ChatSession.created_date.desc()).first()
        if sess_row:
            chat_session_id = sess_row.id
    except Exception as _cs_err:
        logger.debug("resume: chat_session lookup skipped: %s", _cs_err)

    # Build context for tool execution
    ctx = {
        "conversation_id": conversation_id,
        "agent_app_id": agent_app_id,
        "agent_name": agent_name,
        "conversation_metadata": conv.metadata_ or {},
        "chat_session_id": chat_session_id,
        **(data_ctx_extras or {}),
        "endpoint": _resume_eff_llm.endpoint,
    }

    messages = conv.messages or []
    # Remove the partial assistant message that was added during pause
    if messages and messages[-1].get("role") == "assistant" and not messages[-1].get("content"):
        messages = messages[:-1]

    # Execute or skip the pending tool based on approval status
    tool_name = pending_tool["tool_name"]
    args = pending_tool["args"]
    args_str = pending_tool["args_str"]
    tool_call_id = pending_tool["tool_call_id"]
    display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)

    if approval_status == "approved":
        # Re-execute the tool with _skip_permission_check to bypass confirmation
        from app.services.permissions import get_permission_checker, PermissionConfig, PermissionMode
        checker = get_permission_checker()
        original_config = checker.config
        checker.config = PermissionConfig(mode="full_auto")
        try:
            result = await execute_tool(
                tool_name, args, db,
                user.id if user else None,
                context=ctx,
            )
        finally:
            checker.config = original_config

        # Update the tool call record from awaiting_approval to completed/failed
        for tc in tool_calls_for_frontend:
            if tc.get("id") == tool_call_id:
                tc["status"] = "completed" if result.get("success") else "failed"
                tc["results"] = result
                tc.pop("approval_id", None)
                tc.pop("reason", None)
                break

        llm_messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": tool_call_id, "type": "function",
                            "function": {"name": tool_name, "arguments": args_str}}],
        })
        llm_messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result),
        })
    else:
        # Rejected or expired — feed a denial result to the LLM
        reason = f"User rejected the tool call" if approval_status == "rejected" else f"Approval {approval_status or 'unknown'}"
        result = {"success": False, "error": f"Permission denied: {reason}"}
        for tc in tool_calls_for_frontend:
            if tc.get("id") == tool_call_id:
                tc["status"] = "rejected"
                tc["results"] = result
                tc.pop("approval_id", None)
                break

        llm_messages.append({
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": tool_call_id, "type": "function",
                            "function": {"name": tool_name, "arguments": args_str}}],
        })
        llm_messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": json.dumps(result),
        })

    # Execute any remaining calls from the paused batch (they were queued
    # after the pending tool and never ran).
    remaining_calls = pending_tool.get("remaining_calls", [])
    if remaining_calls:
        if len(remaining_calls) == 1:
            remaining_results = [await execute_tool(
                remaining_calls[0]["tool_name"], remaining_calls[0]["args"], db,
                user.id if user else None, context=ctx,
            )]
        else:
            async def _exec_remaining(call):
                return await execute_tool(
                    call["tool_name"], call["args"], db,
                    user.id if user else None, context=ctx,
                )
            raw_remaining = await asyncio.gather(
                *[_exec_remaining(c) for c in remaining_calls],
                return_exceptions=True,
            )
            remaining_results = []
            for i, r in enumerate(raw_remaining):
                if isinstance(r, Exception):
                    remaining_results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
                else:
                    remaining_results.append(r)

        for call, result in zip(remaining_calls, remaining_results):
            _tc_name = call["tool_name"]
            display_name = TOOL_DISPLAY_NAMES.get(_tc_name, _tc_name)
            _tc_record = {
                "id": call["tool_call_id"],
                "name": display_name,
                "arguments_string": call["args_str"],
                "results": result,
                "status": "completed" if result.get("success") else "failed",
            }
            if _tc_name in INTERNAL_TOOLS:
                _tc_record["display_projection"] = _internal_tool_projection(_tc_name)
            tool_calls_for_frontend.append(_tc_record)
            llm_messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": call["tool_call_id"], "type": "function",
                                "function": {"name": call["tool_name"], "arguments": call["args_str"]}}],
            })
            llm_messages.append({
                "role": "tool",
                "tool_call_id": call["tool_call_id"],
                "content": json.dumps(result),
            })

    # Clear resume state and continue the LLM tool-calling loop
    conv.metadata_ = conv.metadata_ or {}
    conv.metadata_.pop("_resume_state", None)
    conv.status = "active"

    assistant_content = ""
    # P0 reliability: per-turn guardrail controller + per-conversation iteration budget
    guard_ctrl = ToolLoopGuardController(_loop_guard_config_for(agent_app))
    _max_iters = getattr(agent_app, "max_call_count", None) or settings.AGENT_MAX_ITERATIONS
    conv_budget = IterationBudget(max_total=_max_iters)
    _verify_attempts = 0
    _pptx_nudge_attempts = 0  # pptx turn-guard nudges this turn (resume)
    _pptx_force_next_iteration = False  # pptx guard one-shot force (resume)
    _file_nudge_attempts = 0   # file turn-guard nudges this turn (resume)
    _file_force_next_iteration = False  # file guard one-shot force
    # Finish-line state (UnboundLocalError discipline). The loop variable
    # shadows the outer ``iteration``, so capture the final value it will take
    # BEFORE the loop rebinds it.
    _final_iteration = iteration + MAX_TOOL_ITERATIONS
    _wrapup_nudged = False
    # ── Dynamic per-turn budget (schema-aware soft cap) ────────────
    # Resumes are user-approval-driven (never unattended automation), so
    # the automation heuristic stays off here. MAX_TOOL_ITERATIONS remains
    # the hard cap; the soft cap can be RAISED mid-loop once describe_schema
    # reveals join edges (see the upgrade hook in the tool-execution branch).
    _resume_base_iter = _final_iteration - MAX_TOOL_ITERATIONS
    _effective_budget = calculate_agent_budget(
        None, user_content or "", is_automation=False,
    )
    _soft_final_iter = _resume_base_iter + _effective_budget
    _schema_edges_seen = False
    for iteration in range(iteration + 1, iteration + 1 + MAX_TOOL_ITERATIONS):
        # P0: consume one iteration from the conversation-level budget
        if not conv_budget.consume():
            logger.info(
                "Conversation %s iteration budget exhausted (%d/%d), breaking",
                conversation_id, conv_budget.used, conv_budget.max_total,
            )
            break
        # ── Dynamic soft-cap check ────────────────────────────────
        # The per-turn budget is a soft cap: the model self-regulates via
        # the T-3 wrap-up nudge below, and we break here if it keeps going.
        if iteration > _soft_final_iter:
            logger.info(
                "v2 resume: dynamic per-turn budget reached (%d) for conv=%s; wrapping up",
                _effective_budget, conversation_id,
            )
            break
        # Per-tool-name runaway guard (same as the main loop above).
        loop_info = _detect_tool_call_loop(llm_messages)
        if loop_info is not None:
            looped_tool, looped_n = loop_info
            # Internal LLM-facing nudge: tells the model to stop
            # calling the same tool and wrap up.
            nudge = (
                f"Tool '{looped_tool}' was already called {looped_n} times. "
                "Use the result you have and produce your final answer. "
                "Do not call it again."
            )
            # User-facing assistant content: friendly text, no
            # internal tool names or call counts. After R4 we tell
            # the user we are proceeding with sensible defaults so
            # the loop guard stopping the agent does not feel like a
            # dead end.
            assistant_content = (
                "I'm going to build the agent with sensible defaults now. "
                "You can adjust anything after creation."
            )
            logger.warning(
                "Tool-call loop guard tripped in resumed conversation %s: "
                "tool=%r count=%d (cap=%d). Breaking loop.",
                conversation_id, looped_tool, looped_n, TOOL_CALL_HARD_CAP,
            )
            llm_messages.append({"role": "user", "content": nudge})
            break
        # Finish line: with 3 iterations left (per the DYNAMIC soft cap),
        # tell the model to stop exploring and assemble its final answer
        # (injected exactly once).
        if iteration == _soft_final_iter - 3 and not _wrapup_nudged:
            _wrapup_nudged = True
            llm_messages.append({
                "role": "user",
                "content": (
                    "You have 3 steps left. Stop exploring and produce "
                    "your final answer with what you have."
                ),
            })
        # PPTX T-3 forcing (resume loop): when budget closing + pptx requested
        # + no artifact created, force create_artifact.
        _pptx_forced = False
        if _pptx_force_next_iteration or should_force_create_pptx(
            user_content,
            tool_calls_for_frontend,
            iteration=iteration - (iteration - MAX_TOOL_ITERATIONS),  # normalize
            max_iterations=MAX_TOOL_ITERATIONS,
            has_artifact_tool="create_artifact" in _tool_names_from_schemas(tools),
        ):
            tool_choice = {
                "type": "function",
                "function": {"name": "create_artifact"},
            }
            _pptx_forced = True
            _pptx_force_next_iteration = False
        # File-deliverable T-3 forcing (resume loop): mirrors pptx for html/
        # docx/pdf/xlsx/md.  Blocked when pptx already forced.
        _file_forced = False
        if not _pptx_forced and (
            _file_force_next_iteration or should_force_create_file(
                user_content,
                tool_calls_for_frontend,
                iteration=iteration - (iteration - MAX_TOOL_ITERATIONS),
                max_iterations=MAX_TOOL_ITERATIONS,
                has_artifact_tool="create_artifact" in _tool_names_from_schemas(tools),
                pptx_forced=_pptx_forced,
            )
        ):
            tool_choice = {
                "type": "function",
                "function": {"name": "create_artifact"},
            }
            _file_forced = True
            _file_force_next_iteration = False
        tool_choice = _compute_tool_choice(
            user_content, data_ctx_extras, iteration,
            tool_names=_tool_names_from_schemas(tools),
        )
        # Finish line: force tool_choice="none" on the final iteration so the
        # LLM must answer in text. No dashboard guard in this loop.
        tool_choice = _finish_line_tool_choice(
            iteration, _final_iteration, _pptx_forced or _file_forced, tool_choice,
        )
        try:
            # P1.3: Pre-API deterministic tool result pruning
            prune_tool_results_only(llm_messages, model=get_model())
            sanitize_messages(llm_messages)
            llm_response = await _call_llm_with_tools(
                llm_messages, tools, tool_choice=tool_choice,
                temperature=llm_overrides.get("temperature"),
                max_tokens=llm_overrides.get("max_tokens"),
            )
        except Exception as e:
            # P1.1: Structured error classification
            ce = classify_api_error(e)
            logger.warning(
                "LLM call error in conversation %s (resume): reason=%s retryable=%s should_compress=%s err=%r",
                conversation_id, ce.reason.value, ce.retryable, ce.should_compress, e,
            )
            metrics.record_error(ce.reason.value)
            assistant_content = f"Sorry, I encountered an error: {ce.message}"
            break

        assistant_content = llm_response.get("content", "")
        raw_tool_calls = llm_response.get("tool_calls", [])

        # ── Clarification hard-stop (:::options) — same as the add_message
        # loop: a message with an options block asks the user a question →
        # turn done, no tools, no gates.
        _opt_names = [tc.get("function", {}).get("name", "") for tc in raw_tool_calls]
        if _options_clarification(assistant_content, _opt_names):
            if raw_tool_calls:
                logger.info(
                    "Suppressing %d tool call(s) after :::options clarification "
                    "block (resume, conv=%s, iter=%d): %s",
                    len(raw_tool_calls), conversation_id, iteration, _opt_names,
                )
                raw_tool_calls = []
            break

        if not raw_tool_calls:
            guardrail_result = _check_hallucination_guardrail(
                user_content, data_ctx_extras, tool_calls_for_frontend,
                iteration, guardrail_retries,
            )
            if guardrail_result.action == "nudge":
                llm_messages.append({"role": "assistant", "content": assistant_content})
                llm_messages.append({"role": "user", "content": guardrail_result.message})
                guardrail_retries += 1
                continue
            elif guardrail_result.action == "fallback":
                assistant_content = guardrail_result.message
                break
            # P2.1: Verification-on-stop
            _verify_nudge = build_verify_on_stop_nudge(llm_messages, attempts=_verify_attempts)
            if _verify_nudge:
                llm_messages.append({"role": "assistant", "content": assistant_content})
                llm_messages.append({"role": "user", "content": _verify_nudge})
                _verify_attempts += 1
                continue
            # P2.1.5: PPTX turn-guard (resume loop) — same deliverable
            # enforcement: nudge (cap 1/turn) or disclose. Runs BEFORE the
            # self-eval gate so a missing deliverable is addressed first.
            _pptx_guard = pptx_turn_guard(
                user_content,
                tool_calls_for_frontend,
                budget_remaining=_final_iteration - iteration,
                attempts=_pptx_nudge_attempts,
            )
            if _pptx_guard.action == "nudge":
                llm_messages.append({"role": "assistant", "content": assistant_content})
                llm_messages.append({"role": "user", "content": _pptx_guard.message})
                _pptx_nudge_attempts += 1
                logger.info(
                    "resume: pptx turn-guard nudge injected (conv=%s, iter=%d)",
                    conversation_id, iteration,
                )
                continue
            if _pptx_guard.action == "disclose":
                logger.warning(
                    "resume: pptx deliverable not generated; disclosure "
                    "appended (conv=%s, iter=%d)", conversation_id, iteration,
                )
                assistant_content = (assistant_content or "") + " " + _pptx_guard.message
            # P2.1.6: File-deliverable turn-guard (resume loop) — mirrors pptx
            # guard for html/docx/pdf/xlsx/md.  Blocked when pptx already nudged.
            if _pptx_guard.action == "none":
                _file_guard = file_turn_guard(
                    user_content,
                    tool_calls_for_frontend,
                    budget_remaining=_final_iteration - iteration,
                    attempts=_file_nudge_attempts,
                )
                if _file_guard.action == "nudge":
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _file_guard.message})
                    _file_nudge_attempts += 1
                    if _file_guard.force_next:
                        _file_force_next_iteration = True
                    logger.info(
                        "resume: file turn-guard nudge injected "
                        "(conv=%s, iter=%d, format=%s, force_next=%s)",
                        conversation_id, iteration,
                        _file_guard.detected_format, _file_guard.force_next,
                    )
                    continue
                if _file_guard.action == "disclose":
                    logger.warning(
                        "resume: file deliverable not generated; disclosure "
                        "appended (conv=%s, iter=%d, format=%s)",
                        conversation_id, iteration, _file_guard.detected_format,
                    )
                    assistant_content = (assistant_content or "") + " " + _file_guard.message
            # P2.2: Universal Self-Evaluation & Re-Planning gate (resume loop)
            _gate_result = await _check_answer_verification_gate(
                user_content,
                tool_calls_for_frontend,
                assistant_content,
                attempts=_gate_attempts,
                budget_remaining=_final_iteration - iteration,
                catalog_meta=(data_ctx_extras or {}).get("catalog_meta"),
            )
            if _gate_result.action == "nudge":
                llm_messages.append({"role": "assistant", "content": assistant_content})
                llm_messages.append({"role": "user", "content": _gate_result.message})
                _gate_attempts += 1
                continue
            if _gate_result.action == "disclose":
                assistant_content = (assistant_content or "") + _gate_result.message
            break

        # Parse + execute tool calls (reuse the parallel execution logic)
        parsed_calls = []
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            tool_name = func.get("name", "")
            args_str = func.get("arguments", "{}")
            tool_call_id = tc.get("id", str(uuid.uuid4()))
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                args = {}
            parsed_calls.append({"tool_name": tool_name, "args": args, "args_str": args_str, "tool_call_id": tool_call_id})

        # Intercept path (R5): same as v2 main — a resumed conversation can
        # also surface a `create_agent` call after the user approved the
        # prior write tool. Intercept and pause for the Decision Summary
        # card; siblings execute normally.
        intercepted, intercept_payload, intercept_index = _intercept_create_agent(parsed_calls)
        if intercepted:
            logger.info(
                "create_agent intercept (v2 resume): conv=%s tool_call_id=%s "
                "payload_keys=%s siblings=%d",
                conversation_id,
                parsed_calls[intercept_index]["tool_call_id"],
                sorted((intercept_payload or {}).keys()),
                len(parsed_calls) - 1,
            )
            sibling_calls = [c for i, c in enumerate(parsed_calls) if i != intercept_index]
            sibling_results: list[dict] = []
            if sibling_calls:
                if len(sibling_calls) == 1:
                    sibling_results = [await execute_tool(
                        sibling_calls[0]["tool_name"],
                        sibling_calls[0]["args"],
                        db,
                        user.id if user else None,
                        context=ctx,
                    )]
                else:
                    raw_sib = await asyncio.gather(
                        *[execute_tool(c["tool_name"], c["args"], db, user.id if user else None, context=ctx) for c in sibling_calls],
                        return_exceptions=True,
                    )
                    for i, r in enumerate(raw_sib):
                        if isinstance(r, Exception):
                            logger.warning("sibling tool '%s' raised: %s", sibling_calls[i]["tool_name"], r)
                            sibling_results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
                        else:
                            sibling_results.append(r)
            for sib_call, sib_result in zip(sibling_calls, sibling_results):
                sib_name = sib_call["tool_name"]
                sib_display = TOOL_DISPLAY_NAMES.get(sib_name, sib_name)
                sib_record = {
                    "id": sib_call["tool_call_id"],
                    "name": sib_display,
                    "arguments_string": sib_call["args_str"],
                    "results": sib_result,
                    "status": "completed" if isinstance(sib_result, dict) and sib_result.get("success") else "failed",
                }
                if sib_name in INTERNAL_TOOLS:
                    sib_record["display_projection"] = _internal_tool_projection(sib_name)
                tool_calls_for_frontend.append(sib_record)
                llm_messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": sib_call["tool_call_id"], "type": "function",
                                    "function": {"name": sib_name, "arguments": sib_call["args_str"]}}],
                })
                llm_messages.append({
                    "role": "tool",
                    "tool_call_id": sib_call["tool_call_id"],
                    "content": json.dumps(sib_result),
                })
            # ReAct reflexion: if any sibling tool failed, inject a
            # critique system message before the next LLM iteration.
            _inject_reflexion_critique(llm_messages, sibling_calls, sibling_results)
            intercepted_call = parsed_calls[intercept_index]
            intercepted_record = {
                "id": intercepted_call["tool_call_id"],
                "name": TOOL_DISPLAY_NAMES.get("create_agent", "create_agent"),
                "arguments_string": intercepted_call["args_str"],
                "status": "awaiting_decision_summary",
            }
            tool_calls_for_frontend.append(intercepted_record)
            _resume_assistant_msg_id = str(uuid.uuid4())
            paused, _stripped, _note = _persist_decision_summary_pause(
                db, conv, messages, _resume_assistant_msg_id,
                tool_calls_for_frontend, assistant_content,
                tool_call_payload=intercept_payload,
            )
            if paused:
                try:
                    db.refresh(conv)
                except Exception:
                    pass
                return conv.to_dict()
            # Sanitiser rejected — fall through to normal flow.

        # P2-12: shared core in app.services.agent_loop.tool_executor.
        async def _invoke_resume(tool_name, args):
            return await execute_tool(tool_name, args, db, user.id if user else None, context=ctx)

        results = await execute_tool_batch(
            parsed_calls,
            before_call=guard_ctrl.before_call,
            invoke=_invoke_resume,
            blocked_result_factory=_guardrail_synthetic_result,
        )

        # Check for approval pause again (a resumed conversation could hit another write tool)
        paused_again = False
        for call, result in zip(parsed_calls, results):
            tool_name = call["tool_name"]
            args_str = call["args_str"]
            tool_call_id = call["tool_call_id"]
            display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)

            if isinstance(result, dict) and result.get("requires_approval"):
                tool_calls_for_frontend.append({
                    "id": tool_call_id, "name": display_name,
                    "arguments_string": args_str, "results": result,
                    "status": "awaiting_approval",
                    "approval_id": result.get("approval_id"),
                    "reason": result.get("reason", ""),
                })
                conv.metadata_["_resume_state"] = {
                    "llm_messages": llm_messages, "iteration": iteration,
                    "tool_calls_for_frontend": tool_calls_for_frontend,
                    "agent_name": agent_name, "agent_app_id": agent_app_id,
                    "data_ctx_extras": data_ctx_extras, "user_content": user_content,
                    "guardrail_retries": guardrail_retries, "tools": tools,
                    "pending_tool": {
                        "tool_name": tool_name, "args": call["args"],
                        "args_str": args_str, "tool_call_id": tool_call_id,
                        "approval_id": result.get("approval_id"),
                        "remaining_calls": parsed_calls[parsed_calls.index(call) + 1:],
                    },
                }
                conv.status = "awaiting_approval"
                assistant_msg = {"id": str(uuid.uuid4()), "role": "assistant", "content": "",
                                 "created_date": datetime.now(timezone.utc).isoformat(), "tool_calls": tool_calls_for_frontend}
                messages.append(assistant_msg)
                conv.messages = messages
                conv.updated_date = datetime.now(timezone.utc)
                db.commit()
                db.refresh(conv)
                return conv.to_dict()

            _tc_record = {
                "id": tool_call_id, "name": display_name,
                "arguments_string": args_str, "results": result,
                "status": "completed" if result.get("success") else "failed",
            }
            if tool_name in INTERNAL_TOOLS:
                _tc_record["display_projection"] = _internal_tool_projection(tool_name)
            tool_calls_for_frontend.append(_tc_record)
            llm_messages.append({"role": "assistant", "content": None,
                                 "tool_calls": [{"id": tool_call_id, "type": "function",
                                                 "function": {"name": tool_name, "arguments": args_str}}]})
            # P0: apply Layer 2 (per-result) persistence + guardrail after_call
            _result_str = _persisted_result_str(
                tool_name, result, conversation_id,
                context_window_tokens=(
                    _resume_eff_llm.endpoint.context_window
                    if _resume_eff_llm.endpoint else None
                ),
            )
            llm_messages.append({"role": "tool", "tool_call_id": tool_call_id, "content": _result_str})
            guard_ctrl.after_call(tool_name, call["args"], _result_str)

            # ── Dynamic budget upgrade ───────────────────────────────
            # describe_schema revealed schema-graph join edges: widen the
            # per-turn soft cap so the agent can explore the joins instead
            # of being nudged to wrap up too early. Idempotent (runs once,
            # on the first edge-bearing result).
            if not _schema_edges_seen and tool_name == "describe_schema":
                _edge_count = _schema_edge_count(result)
                if _edge_count:
                    _schema_edges_seen = True
                    _upgraded_budget = calculate_agent_budget(
                        [{"confidence": 0.9}] * min(_edge_count, 3),
                        user_content or "", is_automation=False,
                    )
                    if _upgraded_budget > _effective_budget:
                        logger.info(
                            "v2 resume: schema graph revealed %d join edge(s); "
                            "per-turn budget %d->%d (conv=%s)",
                            _edge_count, _effective_budget,
                            _upgraded_budget, conversation_id,
                        )
                        _effective_budget = _upgraded_budget
                        _soft_final_iter = _resume_base_iter + _effective_budget

            # P8: Record learning
            try:
                from app.services.learning_graph import record_learning as _rl
                _rl(agent_app_id, f"called {tool_name}",
                    "success" if isinstance(result, dict) and result.get("success") else "failure",
                    context=(user_content or "")[:200], tool=tool_name)
            except Exception:
                pass

        # P0: Layer 3 — apply per-turn aggregate budget to this batch's results
        if not paused_again:
            _batch_ids = [c["tool_call_id"] for c in parsed_calls]
            _batch_names = [c["tool_name"] for c in parsed_calls]
            _apply_turn_budget_to_messages(
                llm_messages, _batch_ids, _batch_names, conversation_id,
                context_window_tokens=(
                    _resume_eff_llm.endpoint.context_window
                    if _resume_eff_llm.endpoint else None
                ),
            )

        # P0: if guardrail controller tripped a halt, inject nudge and break
        if not paused_again and guard_ctrl.halt_decision:
            _hd = guard_ctrl.halt_decision
            logger.warning(
                "Guardrail halt in conversation %s (resume): %s (tool=%s, count=%d)",
                conversation_id, _hd.code, _hd.tool_name, _hd.count,
            )
            metrics.record_guardrail_halt(_hd.code)
            llm_messages.append({
                "role": "user",
                "content": (
                    f"A tool loop was detected: {_hd.message} "
                    "Use the results you already have and produce your final answer."
                ),
            })
            break

        # P0: refund iteration for execute_code turns
        if not paused_again and all(c["tool_name"] == "execute_code" for c in parsed_calls):
            if all(isinstance(r, dict) and r.get("success") is True for r in results):
                conv_budget.refund()

        # ReAct reflexion: if any tool in this batch failed, inject a
        # critique system message so the next iteration reasons about the
        # failure instead of blindly retrying.
        if not paused_again:
            _inject_reflexion_critique(llm_messages, parsed_calls, results)

    # Decision-summary pause (R4): if the resumed loop produced a
    # `:::decision-summary` block, persist the pending payload and
    # short-circuit the normal save path.
    _resume_assistant_msg_id = str(uuid.uuid4())
    paused, _stripped, _note = _persist_decision_summary_pause(
        db, conv, messages, _resume_assistant_msg_id,
        tool_calls_for_frontend, assistant_content,
    )
    if paused:
        try:
            db.refresh(conv)
        except Exception:
            pass
        return conv.to_dict()

    # Build final assistant message
    _msg_id = str(uuid.uuid4())
    assistant_msg = {
        "id": _msg_id, "role": "assistant",
        "content": assistant_content, "created_date": datetime.now(timezone.utc).isoformat(),
    }
    if tool_calls_for_frontend:
        assistant_msg["tool_calls"] = tool_calls_for_frontend
    # Surface create_artifact results as artifacts
    _artifacts = _collect_artifact_results(
        tool_calls_for_frontend, _msg_id, conversation_id, db,
    )
    if _artifacts:
        assistant_msg["artifacts"] = _artifacts
    # Derive and attach the execution trace (Reasoning & actions)
    assistant_msg["trace"] = _derive_trace_from_response(
        assistant_content, tool_calls_for_frontend,
    )
    messages.append(assistant_msg)

    conv.messages = messages
    conv.updated_date = datetime.now(timezone.utc)
    db.commit()
    db.refresh(conv)

    # Fire-and-forget: extract memories from this conversation.
    if agent_app_id and len(messages) >= 4:
        asyncio.create_task(_bg_extract_memories(
            agent_app_id, list(messages), user.id if user else None,
            project_id=getattr(conv, "project_id", None),
        ))

    # P2.3: Background self-improvement review
    try:
        _msg_count = len(messages)
        _review_threshold = DEFAULT_REVIEW_INTERVAL * 4
        if _msg_count >= _review_threshold and _msg_count % _review_threshold < 4:
            spawn_background_review(
                conversation_id,
                list(messages),
                model=getattr(settings, "LLM_MODEL", None),
                api_key=getattr(settings, "LLM_API_KEY", None),
                base_url=getattr(settings, "LLM_BASE_URL", None),
            )
    except Exception:
        pass

    return conv.to_dict()


# ---------------------------------------------------------------------------
# Decision-summary confirm endpoint (R4)
# ---------------------------------------------------------------------------
# Frontend posts to this endpoint when the user clicks "Create Agent" in the
# DecisionSummaryCard. The payload is the (possibly user-edited) draft from
# conv.metadata_["pending_agent_payload"]. We re-sanitise, then call the
# same internal _create_agent helper that the create_agent tool uses, so
# the resulting record is byte-for-byte identical to what the LLM would
# have produced with a tool call.
#
# We also accept an "action" field:
#   {"action": "create", ...}     — execute create_agent (default)
#   {"action": "cancel", ...}      — clear the pending payload so the
#                                   conversation can continue normally
#   {"action": "edit_only", ...}   — update the pending payload without
#                                   creating (so the user can iterate
#                                   before finally creating)


@router.post(
    "/apps/{app_id}/agents/conversations/{conversation_id}/confirm-decision"
)
async def confirm_decision(
    app_id: str,
    conversation_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Confirm (or cancel) a pending Decision Summary.

    Body shape:
      {
        "action": "create" | "cancel" | "edit_only",
        "payload": { ... }   # the AgentApp record fields (overrides the
                             # saved pending_agent_payload so the user
                             # can edit before committing)
      }
    """
    conv = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.is_deleted == False,
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    md = conv.metadata_ or {}
    pending = md.get("pending_agent_payload")
    # `pending` may legitimately be an empty dict (e.g. the LLM emitted
    # a decision summary block with `{}` for testing). Use an explicit
    # `is None` check so we only reject the case where the
    # awaiting_decision_summary flag itself is missing.
    if pending is None and body.get("action") != "cancel":
        raise HTTPException(
            status_code=400,
            detail="No pending decision summary to confirm",
        )

    action = (body.get("action") or "create").lower()
    payload_in = body.get("payload")
    if payload_in is None:
        payload_in = pending or {}
    elif not isinstance(payload_in, dict):
        raise HTTPException(
            status_code=400,
            detail="'payload' must be a JSON object",
        )

    # Re-sanitise the user-edited payload. This applies the same allow-list
    # as the parser path, so even a hostile / malformed edit cannot write
    # arbitrary fields to AgentApp.
    #
    # We MERGE the user-edited payload on top of the saved pending payload
    # so that fields the user did not touch are preserved. The user-edited
    # values win on conflict (and may explicitly clear a field by passing
    # an empty string).
    if isinstance(payload_in, dict) and isinstance(pending, dict):
        merged = {**pending, **payload_in}
    elif isinstance(payload_in, dict):
        merged = payload_in
    else:
        merged = pending or {}
    clean = _sanitize_decision_payload(merged)
    if action == "create" and not clean.get("name"):
        raise HTTPException(
            status_code=400,
            detail="Cannot create agent: 'name' is required",
        )

    if action == "cancel":
        # Clear the pending payload; the conversation can continue.
        new_md = dict(md)
        new_md.pop("pending_agent_payload", None)
        new_md.pop("awaiting_decision_summary", None)
        conv.metadata_ = new_md
        conv.updated_date = datetime.now(timezone.utc)
        db.commit()
        db.refresh(conv)
        return {
            "success": True,
            "action": "cancel",
            "conversation": conv.to_dict(),
        }

    if action == "edit_only":
        # Persist the new draft so the user can iterate.
        new_md = dict(md)
        new_md["pending_agent_payload"] = clean
        new_md["awaiting_decision_summary"] = True
        conv.metadata_ = new_md
        conv.updated_date = datetime.now(timezone.utc)
        db.commit()
        db.refresh(conv)
        return {
            "success": True,
            "action": "edit_only",
            "conversation": conv.to_dict(),
        }

    # action == "create" (or unspecified)
    # Build a friendly confirmation message for the chat thread.
    from app.services.agent_tools import _create_agent as _create_agent_impl

    try:
        result = _create_agent_impl(clean, db, user.id if user else None)
    except Exception as e:
        logger.error("confirm-decision: _create_agent failed: %s", e)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create agent: {e}",
        )

    # Append a confirmation message to the conversation so the user has a
    # visible record of what was created.
    new_msg = {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": (
            f"## Agent Created Successfully\n\n"
            f"**Name**: {clean.get('name')}\n"
            f"**Project**: {clean.get('project', 'global')}\n"
            f"**Model**: {clean.get('model', 'automatic')}\n"
            f"**Agent Type**: {clean.get('agent_type', 'sequential')}\n\n"
            f"You can now open the agent's config page to review and refine "
            f"the five-layer constitutional prompt and bound skills."
        ),
        "created_date": datetime.now(timezone.utc).isoformat(),
        "tool_calls": [
            {
                "id": str(uuid.uuid4()),
                "name": "AgentApp.create",
                "arguments_string": json.dumps(clean, ensure_ascii=False),
                "results": result,
                "status": "completed",
            }
        ],
    }
    messages = list(conv.messages or [])
    messages.append(new_msg)
    conv.messages = messages
    new_md = dict(md)
    new_md.pop("pending_agent_payload", None)
    new_md.pop("awaiting_decision_summary", None)
    conv.metadata_ = new_md
    conv.updated_date = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception as e:
        logger.error("confirm-decision: final commit failed: %s", e)
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail=f"Failed to persist confirmation: {e}",
        )
    db.refresh(conv)

    logger.info(
        "Decision summary confirmed in conv %s → created agent %r (id=%s)",
        conversation_id, result.get("name"), result.get("id"),
    )
    return {
        "success": True,
        "action": "create",
        "agent": result,
        "conversation": conv.to_dict(),
    }


async def _bg_extract_memories(
    agent_app_id: str,
    messages: list[dict],
    user_id: str | None,
    project_id: str | None = None,
) -> None:
    """Fire-and-forget background task to extract memories after a conversation.

    Uses an independent DB session so it doesn't interfere with the
    request-level session that has already closed. Failures are logged
    as warnings and silently swallowed — this must never break the chat.

    ``project_id`` (2026-08-27): forwarded from the conversation so
    extracted memories land in the project-scoped bucket and appear in
    that project's Shared Memory panel.
    """
    try:
        from app.database import SessionLocal
        from app.services.memory_advanced import auto_extract_memories

        # Run in a thread to avoid blocking the event loop with
        # synchronous SQLAlchemy + SQLite operations.
        def _do_extract():
            db = SessionLocal()
            try:
                # auto_extract_memories is async, so we need to run it
                # in an event loop within this thread.
                import asyncio as _asyncio
                loop = _asyncio.new_event_loop()
                try:
                    saved = loop.run_until_complete(
                        auto_extract_memories(
                            db, agent_app_id, messages, user_id,
                            project_id=project_id,
                        )
                    )
                    if saved:
                        logger.info(
                            "Auto-extracted %d memory(ies) for agent %s",
                            len(saved), agent_app_id,
                        )
                finally:
                    loop.close()
            finally:
                db.close()

        import asyncio as _aio
        await _aio.to_thread(_do_extract)
    except Exception as e:
        logger.warning("Background memory extraction failed: %s", e)


async def _call_synthesis_llm(
    system_prompt: str,
    messages: list[dict],
    endpoint: "LLMEndpoint | None" = None,
) -> dict:
    """Single-turn LLM call for report synthesis (no tools).

    Signature matches what ``synthesize_report()`` expects for its
    ``call_llm_fn`` parameter: accepts (system_prompt, [messages]) and
    returns ``{"content": "..."}``.

    When ``endpoint`` is provided (project binding), targets the endpoint's
    base_url / api_key / model_id. Otherwise falls back to the legacy global
    provider (get_model / llm_url / llm_headers).
    """
    full_messages = [{"role": "system", "content": system_prompt}] + messages

    def _clamp_synth_max(
        _max_tokens: int, _ep: "LLMEndpoint | None", _msgs: list[dict]
    ) -> int:
        _clamped = _clamp_max_tokens_for_context(_max_tokens, _ep, messages=_msgs)
        return _clamped if _clamped is not None else _max_tokens

    if endpoint is not None:
        _model = endpoint.model_id
        _url = endpoint.base_url.rstrip("/") + "/chat/completions"
        _headers = {"Authorization": f"Bearer {endpoint.api_key}", "Content-Type": "application/json"}
    else:
        _model = get_model()
        _url = llm_url()
        _headers = llm_headers()
    payload = {
        "model": _model,
        "messages": full_messages,
        # Context-aware clamp (2026-08-26): this path previously requested a
        # FIXED 6144 output tokens regardless of input size. On a grown
        # conversation (e.g. 61k input vs qwen3.6-27b's 65,536 window) that
        # guaranteed a 400 context_overflow on EVERY synthesis call — which
        # is exactly why the empty-bubble guarantee kept falling through to
        # the deterministic "Data Report" template. Same clamp as the
        # stream/tool paths: input + output <= context_window - 512 buffer.
        "max_tokens": _clamp_synth_max(
            getattr(settings, "LLM_SYNTH_MAX_TOKENS", 6144),
            endpoint,
            full_messages,
        ),
    }
    if not model_has_fixed_temperature(_model):
        payload["temperature"] = 0.7

    async def _try_call(trim_rows: bool = False) -> dict:
        _payload = dict(payload)
        if trim_rows:
            # Trim visible rows in user messages to first 30 to reduce context
            _trimmed = [
                {**m, "content": m["content"][:8000]}
                if m.get("role") == "user" and isinstance(m.get("content"), str)
                else m
                for m in _payload["messages"]
            ]
            _payload["messages"] = _trimmed
            # Re-clamp after trimming: smaller input frees output headroom.
            _payload["max_tokens"] = _clamp_synth_max(
                getattr(settings, "LLM_SYNTH_MAX_TOKENS", 6144),
                endpoint,
                _trimmed,
            )
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(_url, headers=_headers, json=_payload)
            _sc = getattr(resp, "status_code", 0)
            if isinstance(_sc, int) and _sc >= 400:
                logger.warning(
                    "Synthesis LLM HTTP %s body: %s",
                    _sc, resp.text[:500],
                )
            resp.raise_for_status()
        data = resp.json()
        choice = data["choices"][0]
        message = choice.get("message", {})
        return {"content": message.get("content", "") or ""}

    try:
        return await _try_call()
    except (httpx.HTTPStatusError, httpx.RequestError) as e:
        logger.warning("Synthesis LLM failed, retrying with trimmed context: %s", e)
        try:
            return await _try_call(trim_rows=True)
        except httpx.HTTPStatusError as e2:
            raise HTTPException(
                status_code=e2.response.status_code,
                detail=f"LLM API error: {e2.response.text}",
            )
        except httpx.RequestError as e2:
            raise HTTPException(status_code=502, detail=f"LLM request failed: {str(e2)}")


async def _generate_dynamic_turn_plan(
    user_content: str,
    kind: str,
    tool_names: list[str],
    endpoint: "LLMEndpoint | None" = None,
) -> "TurnPlan | None":
    """One cheap LLM planning call → a plan tailored to THIS request.

    The user asked for plans that follow their actual input ("revenue +
    inventory dashboard" should plan steps that name revenue and inventory).
    This runs a single no-tool LLM call with a tiny prompt, parses the JSON
    response into a TurnPlan, and returns None on ANY failure (timeout,
    invalid JSON, too few/many steps, network) so the caller falls back to
    the fixed intent template. Never raises; never breaks the turn.
    """
    try:
        from app.services.turn_planner import dynamic_plan_prompt, parse_dynamic_plan

        _sys, _msgs = dynamic_plan_prompt(user_content, kind, tool_names)
        _resp = await asyncio.wait_for(
            _call_synthesis_llm(_sys, _msgs, endpoint=endpoint),
            timeout=getattr(settings, "TURN_PLAN_DYNAMIC_TIMEOUT_S", 20.0),
        )
        _raw = (_resp or {}).get("content") or ""
        _plan = parse_dynamic_plan(_raw, kind)
        if _plan is not None and _plan.steps:
            logger.info(
                "dynamic turn plan (%s): %d steps for '%s…'",
                kind, len(_plan.steps), user_content[:40],
            )
        else:
            logger.info(
                "dynamic turn plan unparsable (%s) for '%s…' — fixed template used",
                kind, user_content[:40],
            )
        return _plan
    except Exception as _dyn_err:  # noqa: BLE001 — planning must never break the turn
        logger.warning(
            "dynamic turn plan failed (%s) — fixed template used: %s",
            kind, _dyn_err,
        )
        return None


async def _call_llm_with_tools(
    messages: list[dict],
    tools: list[dict] | None,
    tool_choice: dict | None = None,
    *,
    model_override: str | None = None,
    endpoint: "LLMEndpoint | None" = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """Call the LLM with optional function calling (tools).

    Args:
        messages: The conversation messages.
        tools: Tool definitions for function calling.
        tool_choice: Optional tool_choice parameter. Set to
            {"type": "function", "function": {"name": "..."}} to force a
            specific tool. Set to None for "auto" (default).
        model_override: Optional model name to override the default (for
            provider fallback).
        endpoint: Optional concrete LLMEndpoint from hierarchical config.
            When set, uses endpoint.base_url / endpoint.api_key /
            endpoint.model_id instead of global settings.
        temperature: Optional temperature override. Defaults to 0.7 when
            not provided. Read from ``UserSetting`` at runtime.
        max_tokens: Optional max_tokens override. Omitted from the payload
            when None to preserve provider defaults.

    Returns:
        dict with:
            - content (str): the text response (may be empty if tool_calls present)
            - tool_calls (list): list of tool call dicts from the LLM
    """
    if endpoint is not None:
        _model = endpoint.model_id
        _url = endpoint.base_url.rstrip("/") + "/chat/completions"
        _headers = {"Authorization": f"Bearer {endpoint.api_key}", "Content-Type": "application/json"}
    else:
        _model = model_override or get_model()
        _url = llm_url()
        _headers = llm_headers()

    payload = {
        "model": _model,
        "messages": apply_cache_control(
            messages,
            enabled=getattr(settings, "PROMPT_CACHE_ENABLED", False),
            cache_ttl=getattr(settings, "PROMPT_CACHE_TTL", "5m"),
        ),
    }
    if not model_has_fixed_temperature(_model):
        payload["temperature"] = temperature if temperature is not None else 0.7
    # Per-model context-aware clamp (replaces the old global hard-cap hack).
    _clamped_max = _clamp_max_tokens_for_context(max_tokens, endpoint, messages=messages, tools=tools)
    if _clamped_max is not None:
        payload["max_tokens"] = _clamped_max
    if tools:
        payload["tools"] = tools
    # Proactively skip tool_choice for models that reject it (vLLM without
    # --enable-auto-tool-choice). Tool calls are recovered from content text.
    _skip_tool_choice = _should_skip_tool_choice(endpoint)
    if tool_choice and not _skip_tool_choice:
        payload["tool_choice"] = tool_choice

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(_url, headers=_headers, json=payload)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        # If tool_choice forcing caused an error (some providers don't
        # support it), retry without tool_choice.
        if tool_choice:
            logger.warning(
                "LLM call with tool_choice failed (%s), retrying without it",
                e.response.status_code,
            )
            payload.pop("tool_choice", None)
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    resp = await client.post(
                        _url, headers=_headers, json=payload
                    )
                    resp.raise_for_status()
            except httpx.HTTPStatusError as e2:
                raise HTTPException(
                    status_code=e2.response.status_code,
                    detail=f"LLM API error: {e2.response.text}",
                )
            except httpx.RequestError as e2:
                raise HTTPException(
                    status_code=502, detail=f"LLM request failed: {str(e2)}"
                )
        else:
            raise HTTPException(
                status_code=e.response.status_code,
                detail=f"LLM API error: {e.response.text}",
            )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {str(e)}")

    data = resp.json()
    choice = data["choices"][0]
    message = choice["message"]
    content = message.get("content", "") or ""
    raw_tool_calls = message.get("tool_calls", [])
    # Fallback: models like qwen3 (without vLLM --enable-auto-tool-choice)
    # embed tool calls in content text instead of the structured field.
    if not raw_tool_calls and content:
        extracted = _extract_tool_calls_from_content(content)
        if extracted:
            logger.info("extracted %d tool_call(s) from content (model=%s)",
                        len(extracted), model_override or "default")
            raw_tool_calls = extracted
            content = _strip_tool_call_markup(content) or content
    # P0: surface provider reasoning (DeepSeek-R1 reasoning_content,
    # Claude thinking, OpenAI o1 reasoning). Returns "" when absent —
    # the v3 SSE emitter treats absence as a no-op.
    reasoning = message.get("reasoning_content") or ""

    return {
        "content": content,
        "tool_calls": raw_tool_calls,
        "reasoning": reasoning or "",
    }


# ---------------------------------------------------------------------------
# Helper: extract tool_calls from assistant content (qwen3, hermes, etc.)
# ---------------------------------------------------------------------------

import uuid as _uuid


def _should_skip_tool_choice(endpoint: "LLMEndpoint | None") -> bool:
    """Proactively omit ``tool_choice`` for models that reject it.

    vLLM served WITHOUT ``--enable-auto-tool-choice`` rejects the
    ``tool_choice`` field with HTTP 400. We used to find out reactively (one
    full round-trip wasted per forced-tool iteration). When the resolved
    endpoint declares ``supports_structured_tool_calls=False`` we just skip the
    field — the model still emits tool calls (as XML in content), which
    ``_extract_tool_calls_from_content`` recovers afterwards.
    """
    if endpoint is not None and not endpoint.supports_structured_tool_calls:
        return True
    return False


def _rebuild_v3_history_messages(
    system_prompt: str,
    messages: list[dict],
) -> list[dict]:
    """Rebuild OpenAI-format ``llm_messages`` from persisted conversation history.

    Persisted assistant messages store tool results inline inside each
    tool_call's ``results`` key rather than as separate ``role: tool``
    messages, so they are re-expanded here to restore the expected
    ``assistant(tool_calls) → tool → ...`` pattern.

    Error/retry persistence can save the SAME tool_call object (same id +
    same embedded results) into more than one assistant message; re-emitting
    it duplicates the tool-result payload in the LLM context and can push a
    turn past the provider's context window.  We dedupe by ``tool_call_id``:
    only the first occurrence of each id is expanded into a ``role: tool``
    message, and stale duplicate assistant messages keep their text but drop
    their already-emitted tool calls.
    """
    llm_messages: list[dict] = [{"role": "system", "content": system_prompt}]
    seen_tool_ids: set[str] = set()
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant" and msg.get("tool_calls"):
            saved_tcs = [
                tc for tc in (msg.get("tool_calls") or [])
                if isinstance(tc, dict)
            ]
            # Drop tool calls whose results were already expanded earlier in
            # the history (stale duplicates re-saved by the error path).
            fresh_tcs = [
                tc for tc in saved_tcs
                if tc.get("id") not in seen_tool_ids
            ]
            # Only calls WITH embedded results can be re-emitted as
            # assistant(tool_calls) → tool. A persisted call without
            # results (the error path saves the frontend display shape;
            # an interrupted turn can also leave one) would otherwise
            # produce a DANGLING assistant tool_call with no following
            # tool response — DeepSeek/OpenAI reject that with 400
            # ("insufficient tool messages following tool_calls").
            complete_tcs = [
                tc for tc in fresh_tcs
                if tc.get("results") is not None
            ]
            openai_tcs = _to_openai_tool_calls(complete_tcs) if complete_tcs else None
            if openai_tcs:
                llm_messages.append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": openai_tcs,
                })
            elif content:
                # Stale duplicate assistant message (same tool calls re-saved
                # by the error path) or result-less calls: keep any visible
                # text, drop the calls.
                llm_messages.append({"role": "assistant", "content": content})
            for tc in complete_tcs:
                tc_id = tc.get("id")
                tc_results = tc.get("results")
                seen_tool_ids.add(tc_id)
                _content = tc_results if isinstance(tc_results, str) else json.dumps(tc_results)
                llm_messages.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "content": _content,
                })
        elif role == "tool":
            _tid = msg.get("tool_call_id", "")
            if _tid and _tid in seen_tool_ids:
                continue  # duplicate of an assistant-embedded result
            if _tid:
                seen_tool_ids.add(_tid)
            llm_messages.append({
                "role": "tool",
                "tool_call_id": _tid,
                "content": msg.get("content", ""),
            })
        elif role and content is not None:
            llm_messages.append({"role": role, "content": content})
    return llm_messages


def _estimate_input_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> int:
    """Rough token estimate for messages + tool definitions.

    Conservative (tends to over-estimate) so ``_clamp_max_tokens_for_context``
    never lets input + output exceed the provider's context window.  Counts
    message ``content`` (string or multimodal text blocks), assistant
    ``tool_calls`` (OpenAI ``function.name/arguments`` AND the persisted
    Zhanlu shape ``name``/``arguments_string`` + embedded ``results``), and
    tool JSON schemas which are sent outside ``messages`` but still consume
    context tokens.  Dense JSON (tool results/arguments) tokenizes at ~1.5-3
    chars/token, well below the 4 chars/token of plain prose, so the whole
    estimate uses 3 chars/token.  Adds a flat +200 overhead for system
    prompt structural tokens (role markers, special tokens, etc.).
    """
    total_chars = 0
    for m in messages:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            # Multimodal content blocks: count text, skip image_url blocks.
            for block in content:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    total_chars += len(block["text"])
        role = m.get("role")
        if isinstance(role, str):
            total_chars += len(role)
        # Assistant tool calls (both OpenAI shape and persisted shape).
        for tc in (m.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function")
            if isinstance(fn, dict):
                total_chars += len(str(fn.get("name") or ""))
                total_chars += len(str(fn.get("arguments") or ""))
            total_chars += len(str(tc.get("name") or ""))
            total_chars += len(str(tc.get("arguments_string") or ""))
            res = tc.get("results")
            if res is not None:
                total_chars += len(res if isinstance(res, str) else json.dumps(res))
    msg_tokens = max(1, total_chars // 3)  # 3 chars/token (dense JSON is costly)

    # Tool definitions: each tool schema is typically 200-600 tokens.
    tool_tokens = 0
    if tools:
        tool_chars = sum(len(str(t)) for t in tools)
        tool_tokens = max(0, tool_chars // 3)

    # Flat overhead for system/structural tokens (role markers, special tokens, chat template)
    overhead = 200

    return msg_tokens + tool_tokens + overhead


def _clamp_max_tokens_for_context(
    max_tokens: int | None,
    endpoint: "LLMEndpoint | None",
    *,
    messages: list[dict] | None = None,
    tools: list[dict] | None = None,
) -> int | None:
    """Clamp ``max_tokens`` so input + output stays within the model's context.

    Three clamping layers (applied in order):

    1. **Per-model max_output_tokens** (``LlmModel.max_output_tokens`` →
       ``LLMEndpoint.max_output_tokens``): hard ceiling regardless of input.
    2. **Dynamic context headroom**: when ``messages`` is provided, estimates
       input tokens (including tool definitions) and reserves a 512-token
       safety buffer so ``input + max_tokens ≤ context_window``.
       Falls back to the old static 20% heuristic when messages are not available.
    3. **Legacy static 20% headroom**: used as a safety net when the caller
       does not provide ``messages``.

    Returns the (possibly reduced) ``max_tokens``, or ``None`` when no
    clamping is possible (missing endpoint / context_window).
    """
    if max_tokens is None or endpoint is None or endpoint.context_window is None:
        # Even without context_window, apply per-model max_output_tokens.
        if max_tokens is not None and endpoint is not None and endpoint.max_output_tokens is not None:
            if max_tokens > endpoint.max_output_tokens:
                logger.info(
                    "clamped max_tokens %d → %d (per-model max_output_tokens for model=%s)",
                    max_tokens, endpoint.max_output_tokens, endpoint.model_id,
                )
                return endpoint.max_output_tokens
        return max_tokens

    effective = max_tokens

    # Layer 1: per-model max_output_tokens ceiling
    if endpoint.max_output_tokens is not None and effective > endpoint.max_output_tokens:
        logger.info(
            "clamped max_tokens %d → %d (per-model max_output_tokens for model=%s)",
            effective, endpoint.max_output_tokens, endpoint.model_id,
        )
        effective = endpoint.max_output_tokens

    # Layer 2: dynamic headroom based on actual input size
    if messages is not None:
        input_est = _estimate_input_tokens(messages, tools=tools)
        # 512-token safety buffer: covers tokenizer differences, special tokens,
        # and chat-template overhead that our rough estimate misses.
        dynamic_max = max(256, endpoint.context_window - input_est - 512)
        if effective > dynamic_max:
            logger.info(
                "clamped max_tokens %d → %d (input_est=%d, context_window=%d for model=%s)",
                effective, dynamic_max, input_est, endpoint.context_window, endpoint.model_id,
            )
            effective = dynamic_max
    else:
        # Layer 3: static 20% headroom fallback
        safe_max = max(256, int(endpoint.context_window * 0.8))
        if effective > safe_max:
            logger.info(
                "clamped max_tokens %d → %d (context_window=%d for model=%s)",
                effective, safe_max, endpoint.context_window, endpoint.model_id,
            )
            effective = safe_max

    return effective


def _extract_tool_calls_from_content(content: str) -> list[dict]:
    """Parse tool calls embedded in assistant content text.

    Some models (e.g. qwen3 without ``--enable-auto-tool-choice`` on vLLM)
    return tool calls as text markup instead of structured ``tool_calls``.
    Supported formats:

    1. **qwen3 XML-style**::

           <function=func_name>
           <parameter=param1>
           value1
           </parameter>
           <parameter=param2>
           value2
           </parameter>
           </function>

    2. **Hermes / Qwen standard**::

           ✅
           {"name": "func_name", "arguments": {"param1": "value1"}}

    3. **Qwen native**::

           Function: func_name
           Arguments: {"param1": "value1"}

    Returns a list of OpenAI-shaped tool_calls dicts, or ``[]`` if none found.
    The caller should only use this when the LLM response has no structured
    ``tool_calls``.
    """
    if not content:
        return []

    tool_calls = []

    # ── Format 1: qwen3 XML-style <function=…><parameter=…>… ──
    xml_pattern = re.compile(
        r"<function=(?P<name>\w+)>(?P<params>.*?)</function>",
        re.DOTALL,
    )
    for m in xml_pattern.finditer(content):
        fn_name = m.group("name")
        params_text = m.group("params")
        args = {}
        param_pattern = re.compile(
            r"<parameter=(?P<key>\w+)>(?P<value>.*?)</parameter>",
            re.DOTALL,
        )
        for pm in param_pattern.finditer(params_text):
            val = pm.group("value").strip()
            try:
                val = json.loads(val)
            except Exception:
                pass
            args[pm.group("key")] = val
        tool_calls.append({
            "id": f"call_{_uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": fn_name,
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        })

    if tool_calls:
        return tool_calls

    # ── Format 2: Hermes-style ✅{…} ──
    hermes_pattern = re.compile(
        r"✅\s*\n?\s*(\{.*?\})",
        re.DOTALL,
    )
    for m in hermes_pattern.finditer(content):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
            fn_name = obj.get("name", "")
            fn_args = obj.get("arguments", {})
            if fn_name:
                tool_calls.append({
                    "id": f"call_{_uuid.uuid4().hex[:24]}",
                    "type": "function",
                    "function": {
                        "name": fn_name,
                        "arguments": json.dumps(fn_args, ensure_ascii=False)
                            if isinstance(fn_args, dict) else str(fn_args),
                    },
                })
        except Exception:
            logger.debug("hermes tool_call parse failed for: %s", raw[:120])

    if tool_calls:
        return tool_calls

    # ── Format 3: Qwen native Function: / Arguments: ──
    qwen_native = re.compile(
        r"Function:\s*(?P<name>\w+)\s*\n\s*Arguments:\s*(?P<args>\{.*?\})",
        re.DOTALL,
    )
    for m in qwen_native.finditer(content):
        fn_name = m.group("name")
        raw_args = m.group("args").strip()
        try:
            fn_args = json.loads(raw_args)
        except Exception:
            fn_args = {}
        tool_calls.append({
            "id": f"call_{_uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": fn_name,
                "arguments": json.dumps(fn_args, ensure_ascii=False)
                    if isinstance(fn_args, dict) else raw_args,
            },
        })

    # ── Format 4: canonical Qwen3 / Hermes-Qwen <tool_call>{json}</tool_call> ──
    # Emitted by vLLM with `--tool-call-parser=qwen3_xml` when the structured
    # tool_calls path is bypassed (e.g. mid-stream truncation, oversized tool
    # schema, or as a defensive fallback in some Qwen3 chat templates).  The
    # inner JSON is the same shape as legacy Format 3 (`Function:/Arguments:`).
    #
    # Whitespace-tolerant to handle observed real-world drift:
    #   - extra whitespace inside the tag (`<  tool_call  >`)
    #   - leading/trailing whitespace around the JSON
    #   - newlines between tag and JSON
    #
    # Per-tag try/except so one malformed block does not poison the rest of
    # the response (vLLM can emit half-finished tags under load).
    _hermes_tag_re = re.compile(
        r"<\s*tool_call\s*>\s*(\{.*?\})\s*<\s*/\s*tool_call\s*>",
        re.DOTALL,
    )
    for _m in _hermes_tag_re.finditer(content):
        raw = _m.group(1).strip()
        try:
            _obj = json.loads(raw)
        except (ValueError, TypeError):
            logger.debug("Format 4: skipping malformed JSON in tool_call tag: %r", raw[:80])
            continue
        if not isinstance(_obj, dict):
            continue
        _fn = _obj.get("name")
        if not isinstance(_fn, str) or not _fn:
            continue
        _args = _obj.get("arguments", {})
        if not isinstance(_args, dict):
            _args = {}
        tool_calls.append({
            "id": f"call_{_uuid.uuid4().hex[:24]}",
            "type": "function",
            "function": {
                "name": _fn,
                "arguments": json.dumps(_args, ensure_ascii=False),
            },
        })

    return tool_calls


def _strip_tool_call_markup(content: str) -> str:
    """Remove tool-call markup and thinking preamble from content text.

    When models (e.g. qwen3 without vLLM --enable-auto-tool-choice) embed
    tool calls as text, the content before the tool call is usually a long
    chain-of-thought monologue.  We strip that preamble when tool calls are
    present because:
    - It wastes context budget if appended to conversation history.
    - It is NOT the user-facing answer (the tool result will produce that).
    - It confuses the answer-verification system.

    The heuristic: if after stripping markup the remaining text is ≤60 chars
    or looks like a thinking monologue (no sentence-ending punctuation in a
    long block), replace it with an empty string so the tool-call iteration
    proceeds with a clean ``assistant_content``.
    """
    # 1) Strip tool-call markup blocks
    # Strip canonical Qwen3 / Hermes-Qwen <tool_call>...</tool_call> blocks
    # FIRST — these blocks contain raw JSON that the existing legacy regexes
    # would partially match (false positive on the inner "name"/"arguments"
    # strings).  Whitespace-tolerant for the same reasons as the extractor.
    clean = re.sub(
        r"<\s*tool_call\s*>.*?<\s*/\s*tool_call\s*>",
        "",
        content,
        flags=re.DOTALL,
    ).strip()
    clean = re.sub(r"<function=\w+>.*?</function>", "", clean, flags=re.DOTALL).strip()
    clean = re.sub(r"✅\s*\n?\s*\{.*?\}", "", clean, flags=re.DOTALL).strip()
    clean = re.sub(r"Function:\s*\w+\s*\n\s*Arguments:\s*\{.*?\}", "", clean, flags=re.DOTALL).strip()

    # 2) If the remaining text is very short, keep it as-is (could be a
    #    real greeting/preamble the user should see).
    if len(clean) <= 60:
        return clean

    # 3) If the text looks like a thinking monologue (many lines, no clear
    #    user-facing answer), truncate it.  Heuristic: >5 newlines or
    #    contains self-referential markers ("I should", "Let me",
    #    "I will", "Wait,", "Proceeding") → strip entirely.
    thinking_markers = re.compile(
        r"I should|I will|Let me|Wait,|Self-Correction|Proceeding|"
        r"Tool call generation|✅|I'll just call it|I need to",
        re.IGNORECASE,
    )
    newline_count = clean.count("\n")
    if newline_count > 5 or thinking_markers.search(clean):
        # Thinking preamble — don't keep it in assistant_content.
        # Return empty so the tool-call iteration is clean.
        return ""

    return clean


# ---------------------------------------------------------------------------
# Helper: convert persisted UI-friendly tool_calls to OpenAI shape
# ---------------------------------------------------------------------------

def _to_openai_tool_calls(saved_tool_calls):
    """Convert frontend-friendly persisted tool_calls to OpenAI shape.

    The persistence layer (see ``agents.py:2060-2067`` and ``3014-3022``)
    stores tool calls with a UI-friendly shape: ``{id, name,
    arguments_string, results, status, display_projection}``.  The
    OpenAI-compatible LLM API (DeepSeek) requires the strict shape
    ``{id, type: 'function', function: {name, arguments}}``.  If we
    feed the persisted shape back into the LLM it returns 400 with
    ``messages[N]: missing field `type```.

    Idempotent: passes through tool_calls already in OpenAI shape.
    Returns None for empty/None input.
    """
    if not saved_tool_calls:
        return None
    out = []
    for tc in saved_tool_calls:
        if not isinstance(tc, dict):
            continue
        # Already in OpenAI shape? Pass through.
        if tc.get("type") == "function" and isinstance(tc.get("function"), dict):
            out.append(tc)
            continue
        name = tc.get("name") or tc.get("function", {}).get("name", "")
        args = tc.get("arguments_string")
        if args is None:
            args = tc.get("function", {}).get("arguments", "{}")
        out.append({
            "id": tc.get("id") or str(uuid.uuid4()),
            "type": "function",
            "function": {
                "name": name,
                "arguments": args if isinstance(args, str) else json.dumps(args),
            },
        })
    return out or None


# ---------------------------------------------------------------------------
# Gap 1: SSE Streaming — streaming LLM helper + v3/stream endpoint
# ---------------------------------------------------------------------------


class LLMStreamError(httpx.HTTPStatusError):
    """An HTTP error from the LLM provider that carries the full body.

    Subclasses ``httpx.HTTPStatusError`` so the existing
    ``except httpx.HTTPStatusError`` retry-without-tool_choice logic
    catches it unchanged. ``request`` is required by the parent
    constructor but is not meaningful for our use, so we pass a
    synthetic request object.
    """

    def __init__(self, status: int, body: str):
        # Build a minimal request/response pair for the parent.
        from httpx import Request, Response
        req = Request("POST", llm_url())
        resp = Response(status_code=status, text=body, request=req)
        super().__init__(
            message=f"LLM returned {status}",
            request=req,
            response=resp,
        )
        self.body = body


def _persist_stream_error(db, conv, conversation_id, messages, e, ce,
                          tool_calls_for_frontend, artifact_ids):
    """Persist an assistant error message so the conversation isn't left
    half-baked (user message saved, no assistant reply), then yield the SSE
    error event. Extracted from the v3 stream handler in Phase 1 — behavior
    unchanged."""
    assistant_content = f"Sorry, I encountered an error: {ce.message}"
    try:
        err_msg = {
            "id": str(uuid.uuid4()), "role": "assistant",
            "content": f"**[Error]** {assistant_content}",
            "created_date": datetime.now(timezone.utc).isoformat(),
        }
        if tool_calls_for_frontend:
            err_msg["tool_calls"] = tool_calls_for_frontend
        # Keep any artifacts produced before the failure reachable on
        # reload (the Artifact rows and message_artifacts links already
        # exist in the DB).
        if artifact_ids:
            err_msg["artifact_ids"] = list(artifact_ids)
        messages.append(err_msg)
        # Use a new list object so SQLAlchemy's change detection picks up
        # the new JSON value (the checkpoint block above may have rebound
        # conv.messages to a different list object).
        conv.messages = list(messages)
        conv.updated_date = datetime.now(timezone.utc)
        db.commit()
        # Re-query a fresh instance so the `done` event's conversation
        # payload reflects the freshly committed row, not a possibly-
        # expunged in-memory reference.
        try:
            conv = db.query(AgentConversation).filter(
                AgentConversation.id == conversation_id,
            ).first() or conv
        except Exception:
            pass
    except Exception as _persist_err:
        logger.warning(
            "v3 stream: failed to persist error message: %s",
            _persist_err,
        )
        db.rollback()
    yield f'data: {json.dumps({"type": "error", "message": str(e), "conversation": conv.to_dict()})}\n\n'


async def _stream_llm_with_tools(
    messages: list[dict],
    tools: list[dict] | None,
    tool_choice: dict | None = None,
    *,
    client: "httpx.AsyncClient | None" = None,
    model_override: str | None = None,
    endpoint: "LLMEndpoint | None" = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
):
    """Stream the LLM response, yielding incremental events.

    This is the streaming-with-tools counterpart to ``_call_llm_with_tools``.
    It uses ``stream: True`` so tokens arrive as they are generated,
    giving the chat UI a typing effect (Gap 1).

    Yields tuples of (event_type, data):
      ("delta", "text chunk")    — stream this text to the client
      ("reasoning", "text")      — provider reasoning_content (DeepSeek-R1)
      ("done", full_content)     — stream complete, no tool_calls returned
      ("tool_calls", assembled)  — stream complete, tool_calls were returned
      ("error", error_message)   — an error occurred (terminal)

    Tool-call fragment reassembly: providers stream ``tool_calls``
    fragmented across chunks (the first chunk carries ``id`` + ``name``,
    later chunks append to ``arguments``). We accumulate fragments by
    ``index`` and emit the assembled list in sorted-index order at
    stream end. This replaces the broken overwrite-at-line-3044 logic
    in the legacy ``_stream_llm_final_response``.

    Args:
        client: Optional injected ``httpx.AsyncClient`` for testing. When
                None, a fresh client is created with the production
                timeout. The client must expose ``__aenter__/__aexit__``
                and ``stream(method, url, **kwargs)`` (standard httpx shape).
        endpoint: Optional concrete LLMEndpoint from hierarchical config.

    The caller is responsible for formatting the yielded events as SSE
    ``data:`` lines.
    """
    if endpoint is not None:
        _model = endpoint.model_id
        _stream_url = endpoint.base_url.rstrip("/") + "/chat/completions"
        _stream_headers = {"Authorization": f"Bearer {endpoint.api_key}", "Content-Type": "application/json"}
    else:
        _model = model_override or get_model()
        _stream_url = llm_url()
        _stream_headers = llm_headers()

    base_payload: dict = {
        "model": _model,
        "messages": messages,
        "stream": True,
    }
    # Live reasoning stream (2026-08-27): when the operator enables
    # LLM_ENABLE_THINKING, ask the provider for its chain-of-thought.
    # vLLM's qwen3 chat template only emits `reasoning` deltas when
    # enable_thinking is explicitly requested.
    if getattr(settings, "LLM_ENABLE_THINKING", False):
        base_payload["chat_template_kwargs"] = {"enable_thinking": True}
    # P1-5: apply per-model tool-output cap to the messages being sent
    # in the streaming path.  Without this, the v3 FSM (which streams
    # multiple LLM calls per turn) can carry forward 50k+ tokens of
    # tool results (e.g. ask_data_agent SQL responses, fetch_data_batch
    # payloads) and overflow small-context models like qwen3.6-27b
    # (65,536).  This complements the same helper applied in
    # llm_service.call_llm() (non-stream path).
    from app.services.compaction.pre_api_prune import smart_truncate
    # P1-5: cap oversized tool outputs before sending.  IMPORTANT: the
    # reasoning_content patch below must apply to THIS truncated list — an
    # earlier version reassigned base_payload["messages"] from the original
    # list and silently discarded the truncation.
    _prepared_messages = smart_truncate(messages, model=_model)
    base_payload["messages"] = _prepared_messages
    if not model_has_fixed_temperature(_model):
        base_payload["temperature"] = temperature if temperature is not None else 0.7
    # Per-model context-aware clamp (replaces the old global hard-cap hack).
    _clamped_max = _clamp_max_tokens_for_context(max_tokens, endpoint, messages=_prepared_messages, tools=tools)
    if _clamped_max is not None:
        base_payload["max_tokens"] = _clamped_max
    if tools:
        # 2026-08-25: BUGFIX — defense in depth. Even though
        # tool_registry.get_schemas() and data_source_runtime now wrap
        # flat schemas in the OpenAI function envelope, some legacy
        # tool-definition sites may still pass through unwrapped
        # schemas. Normalize here so the LLM never sees a malformed tool.
        from app.services.tool_registry import normalize_tools_list
        base_payload["tools"] = normalize_tools_list(tools)
    # Proactively skip tool_choice for models that reject it (vLLM without
    # --enable-auto-tool-choice). Tool calls are recovered from content text.
    _skip_tool_choice = _should_skip_tool_choice(endpoint)
    if tool_choice and not _skip_tool_choice:
        base_payload["tool_choice"] = tool_choice

    # DeepSeek thinking-mode quirk: when the configured model returns
    # `reasoning_content` (e.g. `deepseek-v4-flash`), the NEXT request
    # must echo that reasoning_content back on the prior assistant
    # message. We don't persist reasoning_content in chat_messages
    # today, so when the history is rebuilt from disk the prior
    # assistant message has no `reasoning_content` — DeepSeek rejects
    # the call with 400 "must be passed back to the API". Set an
    # empty string on any prior assistant message that doesn't have
    # one to satisfy the contract; models that don't use thinking
    # mode ignore the field. Mutates a shallow copy so the caller's
    # messages list is untouched.
    base_payload["messages"] = [
        (
            {**m, "reasoning_content": m.get("reasoning_content") or ""}
            if m.get("role") == "assistant" and "reasoning_content" not in m
            else m
        )
        for m in _prepared_messages
    ]

    full_content = ""
    reasoning_acc = ""
    tc_buffer: dict[int, dict] = {}
    last_finish_reason: str | None = None  # Fix 3: diagnostics for empty content

    def _merge_tc_fragment(frag: dict) -> None:
        """Accumulate a streamed tool_call fragment by ``index``.

        The OpenAI/DeepSeek streaming spec sends tool_calls fragmented:
        first chunk for an index carries ``id`` + ``function.name``,
        subsequent chunks append to ``function.arguments`` (a string
        that must be concatenated before json-parsing).
        """
        idx = frag.get("index", 0)
        slot = tc_buffer.setdefault(idx, {
            "id": None, "type": "function",
            "function": {"name": "", "arguments": ""},
        })
        if frag.get("id"):
            slot["id"] = frag["id"]
        if frag.get("type"):
            slot["type"] = frag["type"]
        fn = frag.get("function") or {}
        if fn.get("name"):
            slot["function"]["name"] += fn["name"]
        if fn.get("arguments"):
            slot["function"]["arguments"] += fn["arguments"]

    async def _run_stream(stream_client, payload: dict):
        """Iterate SSE lines from the provider, yielding incremental
        events. Mutates the outer ``full_content`` / ``reasoning_acc``
        / ``tc_buffer`` via ``nonlocal``. Raises ``LLMStreamError`` (a
        subclass of ``httpx.HTTPStatusError``) so the outer wrapper can
        retry without ``tool_choice`` when the provider rejects it.

        On 4xx/5xx, the response body is read INSIDE the streaming
        context (the connection is still open) so the real provider
        error message is preserved — the previous "body unreadable"
        silent failure masked every DeepSeek 400.
        """
        nonlocal full_content, reasoning_acc, last_finish_reason
        try:
            async with stream_client.stream(
                "POST", _stream_url, headers=_stream_headers, json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    # Read the body INSIDE the context (response still
                    # open) so the real provider error is preserved.
                    try:
                        err_body = await resp.aread()
                        err_text = err_body.decode("utf-8", errors="replace") or "(empty body)"
                    except Exception as inner_e:
                        err_text = f"(read failed: {inner_e!r})"
                    if len(err_text) > 1024:
                        err_text = err_text[:1024] + "…"
                    raise LLMStreamError(
                        status=resp.status_code,
                        body=f"{err_text} (status {resp.status_code})",
                    )
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    chunk_raw = line[6:]
                    if chunk_raw.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(chunk_raw)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    fr = choices[0].get("finish_reason")
                    if fr:
                        last_finish_reason = fr
                    delta = choices[0].get("delta", {}) or {}
                    token = delta.get("content", "")
                    if token:
                        full_content += token
                        yield ("delta", token)
                    reasoning = delta.get("reasoning_content") or delta.get("thinking") or delta.get("reasoning") or ""
                    if reasoning:
                        reasoning_acc += reasoning
                        yield ("reasoning", reasoning)
                    for frag in delta.get("tool_calls", []) or []:
                        _merge_tc_fragment(frag)
        except LLMStreamError:
            # Surface to the outer wrapper so it can retry without
            # tool_choice (DeepSeek's thinking mode rejects tool_choice).
            raise
        except httpx.HTTPStatusError as e:
            # Defensive: if raise_for_status slips in (e.g. through a
            # wrapper), convert to LLMStreamError with whatever body
            # we can read while the context is still alive.
            try:
                await e.response.aread()
                err_text = e.response.text or "(empty body)"
            except Exception:
                err_text = "(unreadable)"
            if len(err_text) > 1024:
                err_text = err_text[:1024] + "…"
            raise LLMStreamError(
                status=e.response.status_code,
                body=f"{err_text} (status {e.response.status_code})",
            ) from e

    async def _attempt(stream_client, payload: dict):
        """Run one streaming attempt, yielding events. On
        ``HTTPStatusError`` with ``tool_choice`` set, the caller
        catches and retries without ``tool_choice``."""
        async for ev in _run_stream(stream_client, payload):
            yield ev

    try:
        if client is not None:
            # Test path: injected client, use directly.
            try:
                async for ev in _attempt(client, base_payload):
                    yield ev
            except httpx.HTTPStatusError:
                if not tool_choice or _skip_tool_choice:
                    raise
                logger.warning(
                    "LLM stream with tool_choice failed; retrying without it",
                )
                full_content = ""
                reasoning_acc = ""
                tc_buffer.clear()
                retry_payload = {**base_payload}
                retry_payload.pop("tool_choice", None)
                async for ev in _attempt(client, retry_payload):
                    yield ev
        else:
            # Production path: create and manage client lifecycle.
            async with httpx.AsyncClient(timeout=180.0) as prod_client:
                try:
                    async for ev in _attempt(prod_client, base_payload):
                        yield ev
                except httpx.HTTPStatusError:
                    if not tool_choice or _skip_tool_choice:
                        raise
                    logger.warning(
                        "LLM stream with tool_choice failed; retrying without it",
                    )
                    full_content = ""
                    reasoning_acc = ""
                    tc_buffer.clear()
                    retry_payload = {**base_payload}
                    retry_payload.pop("tool_choice", None)
                    async for ev in _attempt(prod_client, retry_payload):
                        yield ev
    except httpx.HTTPStatusError as e:
        # If the inner stream already read the body and attached it
        # (LLMStreamError.body), surface that — otherwise fall back to
        # trying to read the response body one more time.
        body = getattr(e, "body", None)
        if not body:
            try:
                await e.response.aread()
                body = e.response.text or "(empty body)"
            except Exception:
                body = f"(body unreadable; status {e.response.status_code})"
        if len(body) > 1024:
            body = body[:1024] + "…"
        yield ("error", f"LLM API error: {body}")
        return
    except httpx.RequestError as e:
        yield ("error", f"LLM request failed: {str(e)}")
        return

    if tc_buffer:
        assembled = [tc_buffer[i] for i in sorted(tc_buffer)]
        yield ("tool_calls", assembled)
    else:
        # Fallback: models like qwen3 (without vLLM --enable-auto-tool-choice)
        # embed tool calls in content text instead of streaming tool_call deltas.
        if full_content:
            extracted = _extract_tool_calls_from_content(full_content)
            if extracted:
                logger.info("stream: extracted %d tool_call(s) from content", len(extracted))
                full_content = _strip_tool_call_markup(full_content) or full_content
                yield ("tool_calls", extracted)
                return
        if not full_content:
            logger.warning(
                "LLM returned empty content (finish_reason=%s, conv=%s)",
                last_finish_reason or "unknown", conversation_id,
            )
        yield ("done", full_content)


# Backward-compat alias: the legacy ``_stream_llm_final_response`` name
# is kept so existing tests (test_gaps_1_to_4.py) and any external
# references continue to work. The implementation above supersedes the
# old broken-overwrite version.
_stream_llm_final_response = _stream_llm_with_tools


@router.post("/apps/{app_id}/agents/conversations/{conversation_id}/steer")
async def steer_conversation(
    app_id: str,
    conversation_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Mid-turn steer/interrupt endpoint (P2).

    While the v3 SSE stream is running on this conversation, the user can
    send a steer message that the running loop drains between tool-loop
    iterations and injects into the next LLM call as a user message.
    Returns immediately — does not touch the in-flight stream.

    Body: ``{"message": "..."}`` (non-empty, capped at 8 KB).
    Response: ``{"ok": true, "queued": true}`` on success, 429 when the
    bounded queue is full.
    """
    try:
        message = (body or {}).get("message", "")
    except (AttributeError, TypeError):
        message = ""
    if not isinstance(message, str):
        message = str(message or "")
    message = message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    if len(message) > 8 * 1024:
        raise HTTPException(status_code=400, detail="message too long (max 8 KB)")

    # Verify the conversation exists (404 if not) — best-effort, never break
    # the steer path on a DB hiccup.
    try:
        conv = db.query(AgentConversation).filter(
            AgentConversation.id == conversation_id,
        ).first()
        if conv is None:
            raise HTTPException(status_code=404, detail="conversation not found")
        if conv.created_by_id != user.id and user.role != "admin":
            raise HTTPException(status_code=403, detail="Not authorized to access this conversation")
    except HTTPException:
        raise
    except Exception as _steer_lookup_err:
        logger.warning("steer: conversation lookup failed (non-fatal): %s", _steer_lookup_err)

    ok = steer_bus.enqueue(conversation_id, message)
    if not ok:
        raise HTTPException(status_code=429, detail="steer queue full")

    logger.info(
        "steer: enqueued message (conv=%s, user=%s, len=%d)",
        conversation_id, getattr(user, "id", None), len(message),
    )
    return {"ok": True, "queued": True}


def _chunk_stream_text(text: str, size: int = 180) -> list:
    """Split text into ~``size``-char SSE chunks, preferring newline breaks."""
    if not text:
        return []
    chunks: list = []
    i = 0
    n = len(text)
    while i < n:
        if i + size >= n:
            chunks.append(text[i:])
            break
        j = text.rfind("\n", i, i + size)
        if j > i:
            chunks.append(text[i : j + 1])
            i = j + 1
        else:
            chunks.append(text[i : i + size])
            i += size
    return chunks


# --------------------------------------------------------------------------- #
# Experience layer (Phase A): turn-end recipe learning + user profile updates.
# Best-effort: any failure is logged (never user content or embeddings) and
# skipped, so the chat loop is never affected. Gated by
# RECIPE_LEARNING_ENABLED / USER_PROFILE_ENABLED.
# --------------------------------------------------------------------------- #
def _record_turn_experience(
    *,
    agent_app_id: str,
    user_id,
    user_content: str,
    assistant_content: str,
    tool_sequence=None,
    iterations: int = 0,
) -> None:
    """Turn-end hook: record the tool-sequence recipe + update user profile.

    ``tool_sequence`` is the ordered list of tool call records from this turn
    (each with a ``name``); a non-empty final ``assistant_content`` is the
    success signal for recipe recording. Synchronous + fire-and-forget —
    callers wrap it in try/except for full isolation.
    """
    success = bool(assistant_content and assistant_content.strip())

    if getattr(settings, "RECIPE_LEARNING_ENABLED", False):
        try:
            from app.services.experience_intent import classify_question, INTENT_CONVERSATIONAL
            from app.services.learning_graph import record_recipe

            intent = classify_question(user_content)
            seq = [t.get("name") for t in (tool_sequence or []) if t and t.get("name")]
            if seq and intent != INTENT_CONVERSATIONAL:
                record_recipe(agent_app_id, intent, seq, success, iterations=iterations)
                logger.debug(
                    "experience: recipe recorded agent=%s intent=%s tools=%d success=%s",
                    agent_app_id, intent, len(seq), success,
                )
        except Exception as _rec_err:  # noqa: BLE001 — best-effort
            logger.warning("experience: recipe recording failed (non-fatal): %s", _rec_err)

    if getattr(settings, "USER_PROFILE_ENABLED", False) and user_id:
        try:
            from app.services.user_profile import update_user_profile

            update_user_profile(agent_app_id, user_id, user_content or "")
            logger.debug(
                "experience: user profile updated agent=%s user=%s",
                agent_app_id, user_id,
            )
        except Exception as _prof_err:  # noqa: BLE001 — best-effort
            logger.warning("experience: user profile update failed (non-fatal): %s", _prof_err)


def _store_turn_cache(
    *,
    db,
    agent_app_id: str,
    user_id,
    user_content: str,
    assistant_content: str,
    artifact_ids=None,
) -> None:
    """Turn-end hook: persist a successful answer into the semantic cache.

    Gated by RESPONSE_CACHE_ENABLED. Only substantive answers
    are stored; the cache service itself enforces scope (shared for
    data-driven intents, per-user for conversational) and freshness
    guards. Best-effort: any failure is logged and skipped.
    """
    if not getattr(settings, "RESPONSE_CACHE_ENABLED", False):
        return
    if not assistant_content or not assistant_content.strip():
        return
    try:
        from app.services.experience_intent import classify_question
        from app.services.llm_service import get_embedding
        from app.services.response_cache import store_cached_response

        embedding = get_embedding(user_content)
        if not embedding:
            return  # embedding service unavailable -> cache disabled
        store_cached_response(
            db,
            agent_app_id=agent_app_id,
            user_id=user_id,
            question_text=user_content,
            intent_class=classify_question(user_content),
            embedding=embedding,
            response_content=assistant_content,
            artifact_ids=artifact_ids,
        )
        logger.debug(
            "experience: response cache store attempted agent=%s len=%d",
            agent_app_id, len(assistant_content),
        )
    except Exception as _cache_store_err:  # noqa: BLE001 — best-effort
        logger.warning("experience: response cache store failed (non-fatal): %s", _cache_store_err)


def _resolve_agent_app_key(db: Session, agent_name, app_id: str) -> str:
    """Resolve the agent store key consistent with the chat routes.

    The chat routes key recipes/profiles/cache on ``agent_app.id`` when an
    AgentApp exists, else on ``agent_name``. Keep this identical so the
    feedback endpoint touches the same stores.
    """
    try:
        agent_app = (
            db.query(AgentApp)
            .filter(AgentApp.name == agent_name, AgentApp.is_deleted == False)  # noqa: E712
            .first()
        )
        return agent_app.id if agent_app else (agent_name or app_id or "general_assistant")
    except Exception as _key_err:  # noqa: BLE001 — best-effort
        logger.debug("experience: agent key resolve failed: %s", _key_err)
        return agent_name or app_id or "general_assistant"


@router.post("/apps/{app_id}/agents/conversations/{conversation_id}/messages/{message_id}/feedback")
def add_message_feedback(
    app_id: str,
    conversation_id: str,
    message_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Record explicit thumbs up/down feedback on an assistant message.

    Writes an ``ExperienceEntry`` (entry_type="user_feedback"), then:
      - reinforces/penalizes the matching recipe (Layer 1)
      - reinforces or evicts the matching semantic cache entry (Layer 2)
      - updates the user's explicit profile preferences (Layer 3)

    All adjustments are best-effort and flag-gated (feature flags).
    """
    try:
        rating = int(body.get("rating", 0))
        if rating not in (1, -1):
            raise ValueError("rating must be +1 or -1")
        comment = str(body.get("comment") or "")[:500]
    except (TypeError, ValueError) as _fb_payload_err:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": f"invalid feedback payload: {_fb_payload_err}"},
        )

    conv = (
        db.query(AgentConversation)
        .filter(
            AgentConversation.id == conversation_id,
            AgentConversation.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if conv is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "conversation not found"})

    messages = list(conv.messages or [])
    target_msg = next(
        (
            m for m in messages
            if str(m.get("id")) == message_id and m.get("role") == "assistant"
        ),
        None,
    )
    if target_msg is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "message not found"})

    # Find the preceding user message (the question this answer belongs to).
    user_content = ""
    for m in reversed(messages):
        if str(m.get("id")) == message_id:
            continue
        if m.get("role") == "user":
            user_content = m.get("content") or ""
            break
        if m.get("role") == "assistant":
            break  # hit an earlier assistant message — stop searching

    intent_class = "general"
    try:
        from app.services.experience_intent import classify_question

        intent_class = classify_question(user_content)
    except Exception as _intent_err:  # noqa: BLE001 — best-effort
        logger.debug("feedback: intent classify failed: %s", _intent_err)

    agent_key = _resolve_agent_app_key(db, conv.agent_name, app_id)

    # 1) ExperienceEntry — the durable feedback record.
    try:
        from app.models.experience_entry import ExperienceEntry

        db.add(
            ExperienceEntry(
                agent_app_id=agent_key,
                entry_type="user_feedback",
                summary="user {} assistant message".format("upvoted" if rating > 0 else "downvoted"),
                detail_json=json.dumps(
                    {
                        "message_id": message_id,
                        "comment": comment,
                        "intent_class": intent_class,
                        "question": (user_content or "")[:500],
                    },
                    ensure_ascii=False,
                ),
                outcome="positive" if rating > 0 else "negative",
                confidence=None,
                user_rating=rating,
                user_feedback=comment,
                tags=[intent_class],
            )
        )
        db.commit()
    except Exception as _ee_err:  # noqa: BLE001 — best-effort
        logger.warning("feedback: ExperienceEntry write failed (non-fatal): %s", _ee_err)
        try:
            db.rollback()
        except Exception:
            pass

    # 2) Recipe adjustment (Layer 1).
    if getattr(settings, "RECIPE_LEARNING_ENABLED", False):
        try:
            from app.services.learning_graph import adjust_recipe_feedback

            adjust_recipe_feedback(agent_key, intent_class, rating)
        except Exception as _recipe_fb_err:  # noqa: BLE001 — best-effort
            logger.warning("feedback: recipe adjust failed (non-fatal): %s", _recipe_fb_err)

    # 3) Cache entry adjustment (Layer 2) — thumbs down evicts it.
    if getattr(settings, "RESPONSE_CACHE_ENABLED", False):
        try:
            from app.models.response_cache_entry import ResponseCacheEntry
            from app.services.response_cache import apply_feedback_score, evict_cache_entry

            target_content = target_msg.get("content") or ""
            cache_entry = (
                db.query(ResponseCacheEntry)
                .filter(
                    ResponseCacheEntry.agent_app_id == agent_key,
                    ResponseCacheEntry.response_content == target_content,
                    ResponseCacheEntry.is_deleted == False,  # noqa: E712
                )
                .first()
            )
            if cache_entry is not None:
                if rating < 0:
                    evict_cache_entry(db, cache_entry.id)
                else:
                    apply_feedback_score(db, cache_entry.id, 1)
        except Exception as _cache_fb_err:  # noqa: BLE001 — best-effort
            logger.warning("feedback: cache adjust failed (non-fatal): %s", _cache_fb_err)

    # 4) User profile explicit preferences (Layer 3).
    if getattr(settings, "USER_PROFILE_ENABLED", False):
        try:
            from app.services.user_profile import add_feedback

            add_feedback(agent_key, str(user.id), rating)
        except Exception as _profile_fb_err:  # noqa: BLE001 — best-effort
            logger.warning("feedback: profile adjust failed (non-fatal): %s", _profile_fb_err)

    return {
        "ok": True,
        "rating": rating,
        "intent_class": intent_class,
        "message_id": message_id,
    }


@router.post("/apps/{app_id}/agents/conversations/{conversation_id}/messages/{message_id}/role-feedback")
def add_role_relevance_feedback(
    app_id: str,
    conversation_id: str,
    message_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Record a 1-5 "Relevant to your role?" rating on an assistant message.

    Writes an ``ExperienceEntry`` (entry_type="role_relevance_feedback") with
    the user's role snapshot at feedback time so admins can measure whether
    role-based personalization is working. Distinct from thumbs up/down.
    """
    try:
        rating = int(body.get("rating", 0))
        if rating < 1 or rating > 5:
            raise ValueError("rating must be between 1 and 5")
    except (TypeError, ValueError) as _fb_payload_err:
        return JSONResponse(
            status_code=422,
            content={"ok": False, "error": f"invalid feedback payload: {_fb_payload_err}"},
        )

    conv = (
        db.query(AgentConversation)
        .filter(
            AgentConversation.id == conversation_id,
            AgentConversation.is_deleted == False,  # noqa: E712
        )
        .first()
    )
    if conv is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "conversation not found"})

    messages = list(conv.messages or [])
    target_msg = next(
        (m for m in messages if str(m.get("id")) == message_id and m.get("role") == "assistant"),
        None,
    )
    if target_msg is None:
        return JSONResponse(status_code=404, content={"ok": False, "error": "message not found"})

    agent_key = _resolve_agent_app_key(db, conv.agent_name, app_id)

    # Snapshot the user's role descriptions at feedback time so historical
    # feedback stays interpretable even if roles change later.
    role_snapshot: list[str] = []
    try:
        raw_roles = getattr(user, "role_descriptions", None)
        if raw_roles and isinstance(raw_roles, list):
            role_snapshot = [str(r).strip() for r in raw_roles if str(r).strip()]
    except Exception as _role_err:  # noqa: BLE001 — best-effort
        logger.debug("role-feedback: role snapshot failed: %s", _role_err)

    try:
        from app.models.experience_entry import ExperienceEntry

        db.add(
            ExperienceEntry(
                agent_app_id=agent_key,
                entry_type="role_relevance_feedback",
                summary=f"role-relevance rating {rating}/5",
                detail_json={
                    "message_id": message_id,
                    "rating": rating,
                    "role_snapshot": role_snapshot,
                    "conversation_id": conversation_id,
                },
                outcome="positive" if rating >= 4 else ("neutral" if rating == 3 else "negative"),
                confidence=None,
                user_rating=rating,
                user_feedback=None,
                tags=["role_relevance"],
            )
        )
        db.commit()
    except Exception as _ee_err:  # noqa: BLE001 — best-effort
        logger.warning("role-feedback: ExperienceEntry write failed (non-fatal): %s", _ee_err)
        try:
            db.rollback()
        except Exception:
            pass

    return {
        "ok": True,
        "rating": rating,
        "message_id": message_id,
        "role_snapshot": role_snapshot,
    }


@router.post("/apps/{app_id}/agents/conversations/v3/{conversation_id}/messages/stream")
async def add_message_stream(
    app_id: str,
    conversation_id: str,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user_required),
):
    """Add a message and stream the agent's response via SSE.

    This is the streaming variant of ``add_message`` (v2). It runs the
    same agent loop (system prompt, memory injection, tool calling,
    guardrails) but streams the final LLM text response token-by-token.

    SSE event format:
      ``data: {"type": "tool_progress", "tool_calls": [...]}``  — after each tool iteration
      ``data: {"type": "delta", "content": "..."}``             — streamed text chunk
      ``data: {"type": "done", "content": "...", "conversation": {...}}``  — final event
      ``data: {"type": "error", "message": "..."}``             — error event
      ``data: {"type": "paused", "conversation": {...}}``       — paused for approval
    """
    conv = db.query(AgentConversation).filter(
        AgentConversation.id == conversation_id,
        AgentConversation.is_deleted == False,
    ).first()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conv.created_by_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Not authorized to access this conversation")

    # Wire the authenticated user's role into the request-scoped trace
    # context so RBAC tool-filtering in tool_registry.get_schemas() (which
    # reads TraceContext.current_role()) activates for this request. The
    # SSE generators below inherit this contextvar via asyncio's context
    # copy when _sse_with_heartbeat schedules the stream.
    TraceContext.set(
        session_id=str(conv.id),
        user_id=str(user.id),
        agent_name=conv.agent_name,
        role=user.role,
    )

    # Resolve the owning ChatSession so the FSM route can persist the
    # assistant Message with the right session_id (parity with the v2
    # blocking route ~line 5536). Without this, chat_session_id is a
    # free variable that is never bound in this scope and the assistant
    # message silently fails to persist.
    chat_session_id: str | None = None
    try:
        from app.models.chat_session import ChatSession as _ChatSession
        _sess_row = db.query(_ChatSession).filter(
            _ChatSession.conversation_id == conv.id,
            _ChatSession.is_deleted == False,  # noqa: E712
        ).order_by(_ChatSession.created_date.desc()).first()
        if _sess_row:
            chat_session_id = _sess_row.id
    except Exception as _cs_err:
        logger.debug("add_message_stream: chat_session lookup skipped: %s", _cs_err)

    messages = conv.messages or []
    user_content = body.get("content", "")
    user_role = body.get("role", "user")
    # 2026-08-25: auto-rebind conversation to the dedicated system agent
    # when the user pastes an automation-setup / agent-creation /
    # skill-creation template (EN/ZH/structural).
    _route_to_dedicated_system_agent(conv, user_content, user_role, db)
    selected_skill = body.get("selected_skill") if isinstance(body.get("selected_skill"), dict) else None
    selected_skill_id = body.get("selected_skill_id") or (selected_skill or {}).get("id")
    if selected_skill and selected_skill_id and not selected_skill.get("id"):
        selected_skill["id"] = selected_skill_id
    # Automation-run tagging (2026-08-11): when the executor drives this
    # stream, it tags the body with phase=automation so the frontend can
    # render the prompt as an automation card instead of a user bubble.
    _auto_phase = body.get("phase")
    _auto_task_id = body.get("automation_task_id")
    _auto_exec_id = body.get("automation_execution_id")
    # Per-message model override from the ModelSwitcher (P4 UI).
    # Falls back to settings.LLM_MODEL when absent.
    user_model: str | None = body.get("model")

    # ── Hierarchical LLM resolution ─────────────────────────────────
    # Honor the per-message body project_id (the frontend sends the
    # live-URL selection on every message). A conv created without a
    # project (legacy rows, main-chat entry) must still follow the
    # selected project's configured LLM — otherwise the agent reads
    # the project's data sources but thinks with the default model.
    _llm_eff_pid = resolve_message_project_id(
        db,
        conv_project_id=getattr(conv, "project_id", None),
        body_project_id=body.get("project_id"),
        body_project_name=body.get("project_name"),
    )
    if _llm_eff_pid and not getattr(conv, "project_id", None):
        # One-time heal: persist the binding so later turns and the
        # project's Recent Chats list see this conversation.
        conv.project_id = _llm_eff_pid
    effective_llm = resolve_effective_llm(
        db,
        project_id=_llm_eff_pid,
        agent_name=conv.agent_name,
        user_model=user_model,
        user_is_admin=(user.role == "admin"),
        org_id=conv.org_id,
        app_id=conv.app_id,
    )
    _lock_llm = effective_llm.locked  # used for chat header SSE event
    if effective_llm.locked and user_model:
        logger.info(
            "LLM locked by admin config (reason=%s) — ignoring user model %s",
            effective_llm.locked_reason, user_model,
        )

    # ── User-level LLM overrides (Settings page) ──────────────────────
    llm_overrides = get_user_llm_overrides(db, user.id)

    # Phase 1: file_urls are sent by the frontend's handleAgentSend /
    # handleSend when the user queued attachments. Parse them here so the
    # agent can read what was uploaded. Persisted on the user message so
    # follow-up turns can re-read them (the context assembler pulls them
    # out of conv.messages on the next turn).
    file_urls = body.get("file_urls") or []
    if isinstance(file_urls, str):
        # Tolerate a single url passed as a string
        file_urls = [file_urls]
    # Only allow uploads served by this server — prevents path traversal
    # via arbitrary absolute paths (e.g. /etc/passwd) being injected
    # into the agent context. Combined with _resolve_local_path's
    # upload-root confinement, this closes the arbitrary-file-read hole.
    file_urls = [
        u for u in file_urls
        if isinstance(u, str) and u.startswith("/api/uploads/")
    ]

    user_msg = {
        "id": str(uuid.uuid4()),
        "role": user_role,
        "content": user_content,
        "created_date": datetime.now(timezone.utc).isoformat(),
    }
    if file_urls:
        user_msg["file_urls"] = file_urls
    if selected_skill:
        user_msg["selected_skill"] = selected_skill
    elif selected_skill_id:
        user_msg["selected_skill_id"] = selected_skill_id

    # ── Regenerate flag (2026-08-31) ──────────────────────────────────
    # When the user clicks "Regenerate" on the last assistant message, the
    # frontend re-sends the SAME user content with ``regenerate: true``.
    # We must NOT append a duplicate user bubble to conv.messages — the
    # original user message from the turn being regenerated is already in
    # the list. Instead we reuse it (and its file_urls / skill) so the
    # LLM context and the conversation history stay clean, and the new
    # assistant reply REPLACES the previous one (handled at persist time
    # via ``_replace_last_assistant``).
    _regenerate = bool(body.get("regenerate"))
    if _regenerate:
        user_content, file_urls, selected_skill, selected_skill_id = _resolve_regenerate_turn(
            messages, user_role, user_content, file_urls, selected_skill, selected_skill_id
        )
        logger.info(
            "v3 add_message_stream: regenerate requested (conv=%s) — reusing last user turn",
            conversation_id,
        )
    else:
        messages.append(user_msg)

    # --- Persist the user message immediately ---
    # CRITICAL: commit the user message to the DB BEFORE the tool-calling
    # loop starts. Without this, a crash inside the SSE generator (LLM
    # error, network drop, server kill) leaves the user staring at an
    # empty chat with their message lost — the "not showing anything"
    # symptom. By committing here, the user message is durable and will
    # appear in the conversation history on reload, even if the rest
    # of the agent run never completes.
    conv.messages = list(messages)
    conv.updated_date = datetime.now(timezone.utc)
    try:
        db.commit()
    except Exception as _early_commit_err:
        logger.warning(
            "v3 stream: early user-message commit failed: %s",
            _early_commit_err,
        )
        db.rollback()
        # Don't fail the request — the loop's own error handler can
        # still attempt a persist. But log loudly so we notice.

    # ── Experience layer (Phase B): semantic response cache ──────────────
    # When RESPONSE_CACHE_ENABLED is on, attempt an early cache
    # lookup BEFORE the FSM/tool loop runs. On hit, stream the cached
    # answer through the same SSE delta/done format and skip the LLM run
    # entirely — the "faster on repeat questions" payoff. Strict freshness
    # guards (same data_version + similarity + TTL) live in the cache
    # service; any failure falls back to the normal run. Cached answers
    # are skipped when the turn carries file attachments (the answer
    # would depend on the file contents).
    _cache_hit_entry = None
    if (
        getattr(settings, "RESPONSE_CACHE_ENABLED", False)
        and user_content
        and not file_urls
    ):
        try:
            _cache_agent_app = (
                db.query(AgentApp)
                .filter(AgentApp.name == conv.agent_name, AgentApp.is_deleted == False)  # noqa: E712
                .first()
            )
            if _cache_agent_app is not None:
                from app.services.experience_intent import classify_question
                from app.services.llm_service import get_embedding
                from app.services.response_cache import lookup_cached_response

                _cache_intent = classify_question(user_content)
                _cache_embedding = get_embedding(user_content)
                _cache_hit_entry = lookup_cached_response(
                    db,
                    agent_app_id=_cache_agent_app.id,
                    user_id=str(user.id),
                    question_text=user_content,
                    intent_class=_cache_intent,
                    embedding=_cache_embedding,
                )
        except Exception as _cache_lookup_err:  # noqa: BLE001 — safe fallback
            logger.warning(
                "experience: response cache lookup failed (non-fatal): %s",
                _cache_lookup_err,
            )
            _cache_hit_entry = None

    if _cache_hit_entry is not None:
        # Cache hit — replay the stored answer via SSE and skip the run.
        async def _cache_hit_stream():
            _cached_text = _cache_hit_entry.response_content or ""
            try:
                _cached_msg = {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": _cached_text,
                    "created_date": datetime.now(timezone.utc).isoformat(),
                }
                if _cache_hit_entry.artifact_ids:
                    _cached_msg["artifact_ids"] = _cache_hit_entry.artifact_ids
                conv.messages = list((conv.messages or []) + [_cached_msg])
                conv.updated_date = datetime.now(timezone.utc)
                try:
                    db.commit()
                except Exception:
                    db.rollback()
                logger.info(
                    "experience: response cache HIT agent=%s intent=%s chars=%d",
                    getattr(_cache_hit_entry, "agent_app_id", "?"),
                    getattr(_cache_hit_entry, "intent_class", "?"),
                    len(_cached_text),
                )
                for _chunk in _chunk_stream_text(_cached_text, 180):
                    yield f'data: {json.dumps({"type": "delta", "content": _chunk})}\n\n'
                yield (
                    "data: "
                    + json.dumps({
                        "type": "done",
                        "content": _cached_text,
                        "trace": [],
                        "conversation": conv.to_dict(),
                    })
                    + "\n\n"
                )
            except Exception as _cache_stream_err:  # noqa: BLE001
                logger.warning(
                    "experience: cache hit stream failed (non-fatal): %s",
                    _cache_stream_err,
                )
                try:
                    db.rollback()
                except Exception:
                    pass
                yield (
                    "data: "
                    + json.dumps({
                        "type": "error",
                        "message": "缓存答案回放失败",
                        "conversation": conv.to_dict(),
                    })
                    + "\n\n"
                )

        return StreamingResponse(
            _sse_with_heartbeat(_cache_hit_stream()),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # --- Planning-layer classification (v3 stream) ---
    # When SYNEXIA_FSM_ENABLED is on AND the trigger fires, route the
    # request into the SynexiaFSM and stream ``fsm_state`` events over
    # SSE. The FSM run is sync; we collect its transition events into a
    # list (off the event loop via ``asyncio.to_thread``) and yield them
    # in order. Falls back to the existing tool loop on any error.
    # Best-effort: classifier/FSM failures must never block the SSE stream.
    #
    # Follow-up override (parity with the v2 blocking route): build the
    # conversation context once here. A short refinement ("make it dark
    # theme") would otherwise be bypassed to the context-blind legacy
    # loop; detect it and force the trigger True so the FSM handles it.
    # _v3_conv_ctx is reused by the FSM (via ExecutionRequest) and by the
    # legacy loop's system prompt below — no second DB query.
    _v3_conv_ctx = None
    try:
        _v3_conv_ctx = build_conversation_context(
            db, conversation_id, conv.agent_name or "general_assistant",
        )
    except Exception as _v3_ctx_err:
        logger.debug("v3 build_conversation_context failed (non-fatal): %s", _v3_ctx_err)

    # Compute the data-source runtime context EARLY (before the FSM-vs-
    # legacy branch). Both paths need the same `bound_kb_ids` so the
    # tool calls inside the FSM see project-scoped KB bindings — without
    # this, the FSM path silently drops the KB and `ask_data_agent`
    # reports "no data sources bound" even when the project has a
    # connected database.
    #
    # NOTE: we open a fresh session for this lookup because the
    # request-scoped ``db`` may be in an aborted-transaction state from
    # an earlier failed query (e.g. the early user-msg commit on line
    # 4531). Trying to query on the same session raises
    # ``InFailedSqlTransaction``. Using a short-lived session keeps the
    # call idempotent and side-effect-free.
    _v3_data_ctx_extras: dict = {}
    try:
        from app.database import SessionLocal as _DsrSessionLocal
        from app.services.data_source_runtime import prepare_data_source_runtime
        # Open the dedicated session FIRST — the project-validation
        # queries below need it. Previously we opened it ~60 lines
        # later, after the validation block, which caused a NameError
        # the moment a body with project_id/project_name arrived in
        # production. The silent except path below then set
        # ``_v3_data_ctx_extras = {}`` and the agent saw zero bound
        # KBs even when the project had a connected database.
        _dsr_db = _DsrSessionLocal()
        try:
            from app.models.project import Project as _Project
            # Resolve the project's data-source context per-message so the
            # data-source runtime can extend the agent's bound KBs with
            # the project's KBs even when ``conv.project_id`` is None
            # (e.g. legacy convs from before project-scoping landed, or
            # convs first opened outside a project-scoped entry point).
            # Source-of-truth order: body.project_id (per-message
            # override from the live URL) → conv.project_id (the conv
            # row's stored FK). Validate the body project_id against the
            # live projects table so a stale UUID (admin-deleted project,
            # phantom migration row) doesn't 500 — same defensive
            # treatment as create_conversation above. When the body has
            # project_name but no project_id (or vice versa), resolve
            # the missing side from the projects table so the runtime
            # sees a complete picture.
            _v3_body_pid = body.get("project_id")
            _v3_body_pname = body.get("project_name")
            if _v3_body_pid or _v3_body_pname:
                _v3_validated_row = None
                if _v3_body_pid:
                    _v3_validated_row = (
                        _dsr_db.query(_Project)
                        .filter(_Project.id == _v3_body_pid, _Project.is_deleted == False)  # noqa: E712
                        .first()
                    )
                    if _v3_validated_row is None:
                        logger.debug(
                            "v3 stream: dropping stale body project_id %s "
                            "(project not found or soft-deleted)",
                            _v3_body_pid,
                        )
                        _v3_body_pid = None
                if _v3_validated_row is None and _v3_body_pname:
                    _v3_validated_row = (
                        _dsr_db.query(_Project)
                        .filter(
                            func.lower(_Project.name) == _v3_body_pname.lower(),
                            _Project.is_deleted == False,  # noqa: E712
                        )
                        .first()
                    )
                if _v3_validated_row is not None:
                    _v3_body_pid = _v3_validated_row.id
                    _v3_body_pname = _v3_validated_row.name
            # Body project context takes precedence over conv.project_id
            # so a chat opened from a project page can scope to the
            # project even when the conv row was created without one.
            _v3_effective_pid = _v3_body_pid or getattr(conv, "project_id", None)
            _v3_effective_pname = _v3_body_pname
            if not _v3_effective_pname and _v3_effective_pid:
                _v3_proj_lookup = _dsr_db.get(_Project, _v3_effective_pid)
                _v3_effective_pname = _v3_proj_lookup.name if _v3_proj_lookup else None
            if conv.agent_name:
                _v3_agent_for_dsr = _dsr_db.query(AgentApp).filter(
                    AgentApp.name == conv.agent_name,
                    AgentApp.is_deleted == False,
                ).first()
                if _v3_agent_for_dsr is not None:
                    # Thread the conv's pinned data_source_id (set by the
                    # automation executor on task-pinned runs) so the
                    # data-source runtime's union logic can bind the pinned
                    # KB even when it's absent from agent.knowledge_bases.
                    # Interactive chats have no pin → identical behavior.
                    _v3_pinned_dsr_id = (conv.metadata_ or {}).get(
                        "data_source_id"
                    )
                    _, _, _v3_data_ctx_extras = prepare_data_source_runtime(
                        _dsr_db,
                        _v3_agent_for_dsr,
                        [],
                        "",
                        selected_project_id=_v3_effective_pid,
                        selected_project_name=_v3_effective_pname,
                        pinned_data_source_id=_v3_pinned_dsr_id,
                        user_id=user.id if user else None,
                        user_message=user_content,
                        # P1-5: this is a pre-flight data-ctx bound-KB
                        # check (bound_ids only), so compact mode does
                        # not affect the answer.  Pass None to keep
                        # behaviour 100% unchanged on this path.
                        target_context_window=None,
                    )
                    _v3_bound = _v3_data_ctx_extras.get("bound_kb_ids") or []
                    logger.info(
                        "v3 pre-FSM data ctx: conv=%s agent=%s project=%s "
                        "pinned=%s bound=%d",
                        conv.id, conv.agent_name, _v3_effective_pid,
                        _v3_pinned_dsr_id, len(_v3_bound),
                    )
                    if _v3_pinned_dsr_id and not _v3_bound:
                        logger.warning(
                            "v3 pre-FSM data ctx: pinned data source %s "
                            "resolved to zero bound KBs for conv=%s agent=%s "
                            "— the agent may report 'no data sources bound'",
                            _v3_pinned_dsr_id, conv.id, conv.agent_name,
                        )
        finally:
            _dsr_db.close()
    except Exception as _v3_dsr_err:
        # Promote to WARNING so a future regression of the kind above
        # is visible in production logs (the previous ``logger.debug``
        # call was suppressed when LOG_LEVEL=INFO and the failure was
        # invisible — every chat silently dropped the bound KBs).
        logger.warning(
            "v3 pre-FSM prepare_data_source_runtime failed (non-fatal, "
            "agent will run with bound_kb_ids=[]): %s",
            _v3_dsr_err,
        )
        _v3_data_ctx_extras = {}
        # 2026-08-31: the preflight above assigns `_v3_bound` only inside
        # its try; when prepare_data_source_runtime raises, the except set
        # extras={} but left `_v3_bound` undefined → the very next
        # `bool(_v3_bound)` check below raised UnboundLocalError and the
        # whole planning-trigger block was skipped (non-fatal, but bound-
        # data chats silently lost the FSM/data-bound routing decision).
        _v3_bound = []

    try:
        _v3_plan_trigger = should_trigger_planning(user_content)
        if not _v3_plan_trigger and is_followup_refinement(user_content, _v3_conv_ctx):
            logger.info(
                "v3 add_message_stream: follow-up override — routing "
                "refinement turn to SynexiaFSM (conv=%s)",
                conversation_id,
            )
            _v3_plan_trigger = PlanTrigger(True, 1.0, {"followup": 1}, source="followup-override")
        # Data-bound override: when the agent has bound data sources
        # (bound_kb_ids non-empty) OR the conversation has a pinned
        # data source, always route through SynexiaFSM so quality gates
        # (degenerate-result retry, wrong-grain detection, coverage check,
        # bounce-back guard) are enforced. Exception: non-data intents
        # (greeting/thanks/help/capability) stay on the direct path for
        # speed. No domain keywords — works for any customer's schema
        # and in Chinese.
        _has_data_context = bool(_v3_bound) or bool(_v3_pinned_dsr_id)
        if (
            not _v3_plan_trigger
            and _has_data_context
            and not _is_non_data_intent(user_content)
        ):
            logger.info(
                "v3 add_message_stream: data-bound override — bound=%d pinned=%s, "
                "routing to SynexiaFSM (conv=%s, msg=%.60s)",
                len(_v3_bound), bool(_v3_pinned_dsr_id),
                conversation_id, user_content[:60],
            )
            _v3_plan_trigger = PlanTrigger(
                True, 1.0,
                {"data_bound": len(_v3_bound), "pinned": int(bool(_v3_pinned_dsr_id))},
                source="data-bound-override",
            )
        # Data-tool override: when the agent's tool list contains data tools
        # (ask_data_agent / execute_query / execute_sql / sql_query), the agent
        # is data-capable even if bound_kb_ids is empty (e.g. project-scoped
        # KBs haven't been materialized yet).  Route data queries through the
        # FSM so the quality gates / no-JSON prompt / bounce-back guard fire.
        # Conservative: only fires for unambiguous data intent (≥ 6 words OR
        # contains data/analytics keywords).
        if not _v3_plan_trigger and is_fsm_enabled() and not _is_non_data_intent(user_content):
            from app.services.synexia.data_intent import (
                _DATA_TOOL_NAMES,
                _is_unambiguous_data_query,
                _agent_has_data_tool,
            )
            _agent_tool_names = set(
                (tool.get("name") or tool.get("type") or "")
                for tool in (body.get("tools") or [])
            )
            if (
                _agent_has_data_tool(_agent_tool_names | _DATA_TOOL_NAMES)
                and _is_unambiguous_data_query(user_content)
            ):
                logger.info(
                    "v3 add_message_stream: data-tool override — agent has "
                    "data tools, message is data query → routing to SynexiaFSM "
                    "(conv=%s, msg=%.60s)",
                    conversation_id, user_content[:60],
                )
                _v3_plan_trigger = PlanTrigger(
                    True, 1.0,
                    {"data_tools": 1},
                    source="data-tool-override",
                )
        # Automation force-planning (Phase 5 — Manus parity): when the caller
        # sets ``force_planning`` in the body (the automation executor does
        # this for every scheduled run), bypass the classifier and route
        # unconditionally through SynexiaFSM. Manus always plans before acting
        # on a scheduled job; the classifier can otherwise bypass planning for
        # "simple-looking" prompts. Has no effect on normal chat turns (the
        # frontend never sends this flag).
        if not _v3_plan_trigger and bool(body.get("force_planning")) and is_fsm_enabled():
            logger.info(
                "v3 add_message_stream: force_planning set — routing to "
                "SynexiaFSM unconditionally (conv=%s)",
                conversation_id,
            )
            _v3_plan_trigger = PlanTrigger(True, 1.0, {"forced": 1}, source="automation-forced")
        logger.debug(
            "v3 add_message_stream: planning trigger confidence=%.2f signals=%s fsm_enabled=%s",
            _v3_plan_trigger.confidence,
            _v3_plan_trigger.signals,
            is_fsm_enabled(),
        )
        if _v3_plan_trigger and is_fsm_enabled():

            async def _fsm_event_stream():
                """Yield fsm_state events, stream the FSM final response
                token-by-token, then emit done.

                The FSM runs through VERIFY with ``generate_response=False``
                (so FINALIZE defers the response prompt) and the final
                response is streamed live below — giving FSM-routed turns
                the same token-streaming UX as ReAct turns.
                """
                collected: list[str] = []
                # Typed live-activity events for this turn: appended alongside
                # their SSE frames so the assistant Message persists the same
                # structured feed (collapsed summary on reload).
                live_events: list[dict] = []
                _live_event_count = [0]

                def _push_live(event_type: str, label_key: str, params: dict | None = None) -> None:
                    """Build a live event, append its SSE frame, record for persistence."""
                    _ev = _build_live_event(event_type, label_key, params, count=_live_event_count)
                    if _ev is not None:
                        collected.append(_sse_live_event(_ev))
                        live_events.append(_ev)

                def _on_state(state: str) -> None:
                    collected.append(_emit_fsm_state(state))
                    collected.append(_emit_phase(state))
                    # Live feed: typed phase_enter for every FSM transition.
                    _push_live("phase_enter", f"phase_enter.{state}")
                    # FINALIZE is only entered after VERIFY runs — the honest
                    # pass/fail verdict is emitted by ``_on_verify`` during the
                    # VERIFY state (verify_passed only when the checks really
                    # passed), then finalize_started marks the synthesis stage.
                    if state == "finalize":
                        _push_live("finalize_started", "finalize_started")

                def _on_verify(passed: bool, _result=None) -> None:
                    # Honest verdict: never claim "Verification passed" when
                    # checks failed (e.g. a sandbox/artifact node failed).
                    try:
                        _push_live(
                            "verify_passed" if passed else "verify_failed",
                            "verify_passed" if passed else "verify_failed",
                        )
                    except Exception as _v_err:
                        logger.debug("on_verify SSE emit failed (non-fatal): %s", _v_err)

                _v3_agent_name = conv.agent_name or "general_assistant"
                _v3_org_id = getattr(conv, "org_id", None) or "default-org"
                _v3_app_id = app_id or "default-app"

                _fsm_req = ExecutionRequest(
                    conversation_id=conversation_id,
                    agent_name=_v3_agent_name,
                    user_message=user_content,
                    user_id=str(getattr(user, "id", None)) if user else None,
                    # Phase 1: forward the user's uploaded files so the FSM's
                    # context_assembler can extract their text and fold it
                    # into the LLM prompt (see context_assembler.assemble_context).
                    attachments=file_urls,
                    mode="dynamic",
                    org_id=_v3_org_id,
                    app_id=_v3_app_id,
                    conversation_context=_v3_conv_ctx,
                    selected_skill=selected_skill,
                    # Forward project-scoped data sources to the FSM so
                    # tool calls inside the FSM (ask_data_agent,
                    # list_data_sources, execute_query) see the same
                    # bound_kb_ids the v2 tool loop would have set up.
                    data_ctx_extras=_v3_data_ctx_extras or None,
                    # Mark automation runs (force_planning is only sent by
                    # the automation executor) so the quality gate uses the
                    # stricter unattended threshold (0.6 vs 0.4).
                    is_automation=bool(body.get("force_planning")),
                    # Hierarchical LLM pin (project/agent → llm_models):
                    # every LLM call inside the FSM now uses the resolved
                    # endpoint instead of the legacy .env defaults — so the
                    # badge model and the runtime model finally match on
                    # FSM-routed turns.
                    endpoint=effective_llm.endpoint,
                )
                _fsm = SynexiaFSM(db)

                # Per-node activity-step emitter (Phase 5 — Manus parity).
                # The FSM runs ACT/OBSERVE on a worker thread; this callback
                # appends activity_step SSE strings to ``collected`` (same
                # thread-safe pattern as ``_on_state``) so a watcher sees the
                # plan execute step-by-step — and self-correct via the ACT
                # re-plan — instead of a blank feed until the response streams.
                _node_step_nums: dict[str, int] = {}
                _next_step = [0]

                def _on_plan_node(node_dict, status: str, detail=None) -> None:
                    try:
                        name = (node_dict or {}).get("name") or "step"
                        # Assign a stable step number per node name on first
                        # sight (the "running" transition); reuse it for the
                        # completion/failure/re-plan update so the executor's
                        # number-keyed replace updates the same step row.
                        if name not in _node_step_nums:
                            _next_step[0] += 1
                            _node_step_nums[name] = _next_step[0]
                        num = _node_step_nums[name]
                        ntype = (node_dict or {}).get("node_type") or ""
                        desc = name if not ntype else f"{name}"
                        tool_name = name if ntype in ("tool", "skill") else None
                        # Map FSM node status → activity-step status. "running"
                        # marks the step in-progress; everything else finalizes
                        # it (done), with the error surfaced as detail.
                        step_status = "running" if status == "running" else "done"
                        collected.append(_emit_activity_step(
                            num, desc, step_status,
                            tool_name=tool_name,
                            detail=detail,
                        ))
                        # Live feed: typed tool events for tool/skill nodes —
                        # spinner on start, checkmark (or failure/retry) on
                        # terminal status. Structured params only.
                        if ntype in ("tool", "skill"):
                            if status == "running":
                                _push_live("tool_call_started", "tool_call_started", {"tool_label": name})
                            elif status == "completed":
                                _push_live("tool_call_finished", "tool_call_finished", {"tool_label": name})
                            elif status in ("failed", "denied", "skipped"):
                                _push_live("tool_call_failed", "tool_call_failed", {"tool_label": name})
                            elif status == "replanning":
                                _push_live("retry", "retry", {"target": name})
                        elif ntype == "plan" and status == "running":
                            # Plan summary card. Step labels come from
                            # node_dict.children[*].name when the plan DAG is
                            # available; otherwise we emit just the count.
                            _children = (node_dict or {}).get("children") or []
                            _step_names = [
                                str(c.get("name") or c.get("description") or "").strip()
                                for c in _children if isinstance(c, dict)
                            ]
                            _step_names = [s for s in _step_names if s]
                            _push_live(
                                "plan_summary", "plan_summary",
                                {"n": len(_step_names) or 1, "steps": _step_names[:8]},
                            )
                    except Exception as _cb_err:  # noqa: BLE001 — observability only
                        logger.debug("on_plan_node SSE emit failed (non-fatal): %s", _cb_err)

                try:
                    fsm_result = await asyncio.to_thread(
                        _fsm.run, _fsm_req, _on_state, False, _on_plan_node,
                        # generate_response=False, on_plan_node=_on_plan_node
                        # on_verify=_on_verify — honest verify_passed/failed
                        # verdicts for the live feed (keyword for clarity).
                        on_verify=_on_verify,
                    )
                except Exception as _fsm_err:
                    logger.error(
                        "v3 FSM route failed (conv=%s): %s — falling through to tool loop",
                        conversation_id, _fsm_err,
                    )
                    yield _emit_fsm_state("fail")
                    yield _emit_phase("fail")
                    raise

                for ev in collected:
                    yield ev

                # Emit plan_summary as a standalone SSE event so the
                # frontend can render the decomposed execution plan
                # (nodes, status) before the final response streams in.
                if fsm_result.plan_summary:
                    _le_plan = _build_live_event(
                        "plan_preview", "plan_preview",
                        {"n": len(fsm_result.plan_summary)}, count=_live_event_count,
                    )
                    if _le_plan is not None:
                        live_events.append(_le_plan)
                        yield _sse_live_event(_le_plan)
                    yield f'data: {json.dumps({"type": "plan_summary", "plan": fsm_result.plan_summary})}\n\n'

                # Stream the FINALIZE response token-by-token (like ReAct turns).
                assistant_content = ""
                try:
                    async for _et, _data in _fsm.stream_final_response(_fsm_req):
                        if _et == "delta":
                            assistant_content += _data
                            yield f'data: {json.dumps({"type": "delta", "content": _data})}\n\n'
                        elif _et == "done":
                            assistant_content = _data
                except Exception as _stream_err:
                    logger.warning("v3 FSM response streaming failed (non-fatal): %s", _stream_err)
                    assistant_content = fsm_result.assistant_content or assistant_content
                if not assistant_content:
                    assistant_content = fsm_result.assistant_content or ""

                # QUALITY_EVAL (Tier 2 — Approach C): post-stream semantic
                # quality critique + corrective re-generation.  Runs in a
                # worker thread (call_llm is sync).  For automation
                # (unattended), replacing the streamed text with a revised
                # version is fine — no live user is watching tokens.  The
                # final persisted Message gets the (possibly revised) text.
                # Non-fatal: any failure leaves assistant_content unchanged.
                try:
                    _qer = await asyncio.to_thread(
                        _fsm.run_quality_eval, _fsm_req, assistant_content,
                    )
                    if _qer is not None:
                        if _qer.final_text and _qer.final_text != assistant_content:
                            assistant_content = _qer.final_text
                        yield f'data: {json.dumps({"type": "quality_eval", "quality_eval": _qer.to_dict()})}\n\n'
                except Exception as _qe_err:  # noqa: BLE001 — observability only
                    logger.warning("QUALITY_EVAL streaming hook failed (non-fatal): %s", _qe_err)

                # Kimi/GPT-style citations: collect the data sources the
                # FSM actually queried this turn (source_id/source_name
                # from tool result_data). Attached to the persisted
                # assistant message + the done payload so the frontend
                # can render source chips under the bubble.
                _fsm_citations = _extract_citations_from_tool_calls(
                    fsm_result.tool_calls or []
                )

                # Persist the assistant message and emit done.
                try:
                    from app.models.chat_message import ChatMessage as Message
                    # On regenerate, the frontend REUSES the existing
                    # assistant ChatMessage row (same id, content updated
                    # in place via ChatMessage.update), so creating a
                    # fresh row here would leave a stale duplicate. Skip.
                    if chat_session_id and not _regenerate:
                        msg = Message(
                            id=str(uuid.uuid4()),
                            conversation_id=conversation_id,
                            session_id=chat_session_id,
                            role="assistant",
                            content=assistant_content,
                            agent_name=_v3_agent_name,
                            trace=fsm_result.plan_summary or {},
                            reasoning=None,
                            live_events=live_events or None,
                            sources=_fsm_citations or None,
                        )
                        db.add(msg)
                        db.commit()
                        db.refresh(msg)
                except Exception as _persist_err:
                    logger.warning("v3 FSM route: assistant persist failed (non-fatal): %s", _persist_err)

                # Persist the assistant reply into conv.messages too, so the
                # next turn's context assembler sees BOTH sides of the
                # conversation (parity with the v2 blocking route at
                # ~line 1743).  Without this, follow-up turns see only the
                # user's messages and lose all artifact/context references.
                try:
                    _compact_tool_calls = []
                    for _tc in (fsm_result.tool_calls or []):
                        _compact_tool_calls.append({
                            "name": _tc.get("name"),
                            "success": _tc.get("success"),
                            "artifact_ids": _tc.get("artifact_ids", []),
                        })
                    _assistant_msg = {
                        "id": str(uuid.uuid4()),
                        "role": "assistant",
                        "content": (assistant_content or "")[:4000],
                        "created_date": datetime.now(timezone.utc).isoformat(),
                        "tool_calls": _compact_tool_calls,
                        "artifact_ids": fsm_result.artifact_ids or [],
                        "file_exports": (
                            fsm_result.file_exports if hasattr(fsm_result, "file_exports") else {}
                        ),
                        "quality_eval": (
                            fsm_result.quality_eval if hasattr(fsm_result, "quality_eval") else None
                        ),
                        "sources": _fsm_citations,
                    }
                    # On regenerate the old assistant reply was popped at
                    # the top of the handler, so appending yields exactly
                    # one assistant message per user turn (Kimi/GPT parity).
                    messages.append(_assistant_msg)
                    conv.messages = list(messages)
                    conv.updated_date = datetime.now(timezone.utc)
                    db.commit()
                    db.refresh(conv)
                except Exception as _conv_msg_err:
                    logger.warning(
                        "v3 FSM route: conv.messages append failed (non-fatal): %s",
                        _conv_msg_err,
                    )
                    db.rollback()

                # --- Experience layer (Phase A): turn-end recipe + user profile ---
                try:
                    _fsm_agent_app = (
                        db.query(AgentApp)
                        .filter(AgentApp.name == _v3_agent_name, AgentApp.is_deleted == False)  # noqa: E712
                        .first()
                    )
                    _fsm_agent_app_id = _fsm_agent_app.id if _fsm_agent_app else _v3_agent_name
                    _fsm_tool_seq = []
                    for _tc in (fsm_result.tool_calls or []):
                        _nm = _tc.get("name") if isinstance(_tc, dict) else getattr(_tc, "name", "")
                        if _nm:
                            _fsm_tool_seq.append({"name": _nm})
                    _record_turn_experience(
                        agent_app_id=_fsm_agent_app_id,
                        user_id=getattr(user, "id", None),
                        user_content=user_content,
                        assistant_content=assistant_content,
                        tool_sequence=_fsm_tool_seq,
                        iterations=len(_fsm_tool_seq),
                    )
                    _store_turn_cache(
                        db=db,
                        agent_app_id=_fsm_agent_app_id,
                        user_id=getattr(user, "id", None),
                        user_content=user_content,
                        assistant_content=assistant_content,
                        artifact_ids=getattr(fsm_result, "artifact_ids", None),
                    )
                except Exception as _fsm_xp_err:  # noqa: BLE001 — best-effort
                    logger.warning("experience: FSM turn-end hook failed (non-fatal): %s", _fsm_xp_err)

                done_payload = {
                    "type": "done",
                    "content": assistant_content,
                    "conversation": conv.to_dict(),
                    "fsm_state": fsm_result.state,
                    "fsm_execution_id": fsm_result.execution_id,
                    "fsm_confidence": fsm_result.confidence,
                    "fsm_tool_calls": fsm_result.tool_calls,
                    "fsm_artifact_ids": fsm_result.artifact_ids,
                    # Phase 5: surface the quality-gate decision + plan summary
                    # so the automation executor can apply the gate to its own
                    # deliverable (generated via document_generator) and so the
                    # panel can render the decomposed plan. Both are None when
                    # the FSM produced no artifacts / no plan.
                    "fsm_quality_gate": fsm_result.quality_gate,
                    "fsm_plan_summary": fsm_result.plan_summary,
                    "sources": _fsm_citations,
                }
                _push_live("finalize_done", "finalize_done")
                yield f'data: {json.dumps(done_payload)}\n\n'

            return StreamingResponse(
                _sse_with_heartbeat(_disconnect_safe_stream(_fsm_event_stream)),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    # Bug 3 fix: explicit keep-alive header for proxies
                    # that don't infer it from Content-Type. Some
                    # corporate reverse proxies close the connection
                    # after one HTTP request unless they see this.
                    "Connection": "keep-alive",
                },
            )
    except Exception as _v3_plan_err:
        logger.warning(
            "v3 add_message_stream: planning trigger check failed (non-fatal): %s",
            _v3_plan_err,
        )

    # --- Pre-run setup (mirrors add_message v2) ---
    # P3-bis: when ``conv.agent_name`` is None (the case for sessions
    # auto-adopted by ``_create_automation`` — the actual executor is
    # ``automation_runtime_agent``, not the chat agent), default to
    # ``general_assistant`` so the chat's toolset and system prompt
    # resolve to a real agent. Without this, ``get_tools`` falls back
    # to a minimal user-agent toolset that doesn't include
    # ``execute_automation`` — the LLM then tells the user "I don't
    # have execute_automation" when the /automation "Run Now" button
    # hands a structured "Run Automation Task: ..." prompt to the
    # chat. Mirrors the v3 path's ``_v3_agent_name = conv.agent_name
    # or "general_assistant"`` default a few hundred lines above.
    agent_name = conv.agent_name or "general_assistant"
    agent_app = None
    tool_config = None
    if agent_name:
        agent_app = db.query(AgentApp).filter(
            AgentApp.name == agent_name,
            AgentApp.is_deleted == False,
        ).first()
        if agent_app and agent_app.tool_config:
            tool_config = agent_app.tool_config

    system_prompt = get_system_prompt(agent_name, agent_app, user_message=user_content)
    agent_app_id = agent_app.id if agent_app else agent_name

    # ── 2026-08-25: Project Knowledge Cache -- Qwen fast-path ─────────
    # If the project's knowledge cache hits AND the model is Qwen, inject
    # a structured cache block at the top of the system prompt. The block
    # tells the LLM the answer is already in context, so it can skip
    # schema-linker / NL2SQL for product / entity / metric questions.
    # Behaviour: off by default. Fails open -- never raises.
    try:
        from app.services.project_knowledge.fast_path import try_fast_path, build_cached_system_block
        from app.config import settings as _settings
        _project_id_for_cache = (
            agent_app.project_id if agent_app and getattr(agent_app, "project_id", None)
            else None
        )
        if (
            _project_id_for_cache
            and _settings.PROJECT_KNOWLEDGE_QWEN_FAST_PATH
            and _settings.PROJECT_KNOWLEDGE_CACHE_ENABLED
        ):
            _cached = try_fast_path(
                db,
                _project_id_for_cache,
                user_content,
                model_id=locals().get("resolved_model_id") or locals().get("llm_model_id") or None,
            )
            if _cached is not None:
                system_prompt = (
                    system_prompt + "\n\n" + build_cached_system_block(_cached)
                )
                logger.info(
                    "project_knowledge fast-path hit: project=%s kind=%s confidence=%.2f",
                    _project_id_for_cache, _cached.kind, _cached.confidence,
                )
    except Exception as _fp_err:
        logger.debug("project_knowledge fast-path skipped (non-fatal): %s", _fp_err)

    # Resolve the chat session that owns this conversation so tools like
    # ``_create_automation`` can link the new task to the chat. The LLM
    # cannot pass chat_session_id in its tool args (it doesn't know it).
    # Without this lookup the "Scheduled" button never appears in the chat
    # header for sessions whose automations were created via the agent.
    chat_session_id: str | None = None
    try:
        from app.models.chat_session import ChatSession
        sess_row = db.query(ChatSession).filter(
            ChatSession.conversation_id == conv.id,
            ChatSession.is_deleted == False,  # noqa: E712
        ).order_by(ChatSession.created_date.desc()).first()
        if sess_row:
            chat_session_id = sess_row.id
    except Exception as _cs_err:
        logger.debug("add_message_stream: chat_session lookup skipped: %s", _cs_err)

    # P8: Unified system prompt assembly (memory + todos + coding context + learning graph)
    try:
        from app.services.dynamic_prompt_builder import build_system_prompt as _build_prompt
        system_prompt = _build_prompt(
            base_prompt=system_prompt,
            db=db,
            agent_app_id=agent_app_id,
            conversation_id=conversation_id,
            user_id=user.id if user else None,
            agent_app=agent_app,
            # Project scope (2026-08-05): forward the effective
            # project_id (body override > conv.project_id, resolved
            # up-front in the data-source-runtime prep block) so the
            # memory snapshot is recalled only from the active
            # project. Without this, a note like "Q2 2026 sales
            # report" taken in one project leaked into every other
            # project's system prompt — the user saw the same
            # report-recollection greeting in convs across all
            # projects. The same ``_v3_effective_pid`` / fallback
            # name resolution is used for the data-source runtime
            # above, so both pieces see the same project scope.
            project_id=_v3_effective_pid,
        )
    except Exception:
        pass

    # Inject conversation context (follow-up awareness) so the legacy
    # streaming loop resolves refinement turns ("make it dark theme")
    # against prior artifacts instead of treating them as brand-new topics.
    try:
        _v3_followup_block = format_followup_context_block(_v3_conv_ctx)
        if _v3_followup_block:
            system_prompt += _v3_followup_block
    except Exception:
        pass  # Follow-up context injection is best-effort

    # Phase 1: inject extracted attachment text into the system prompt so
    # the legacy tool loop (non-FSM path) can read what the user uploaded.
    # The FSM path gets the same content via context_assembler, but the
    # legacy loop never calls assemble_context — so we inline it here.
    # Image files with no OCR text are noted as "[image attached — use the
    # multimodal content block]" — the actual image bytes are forwarded as
    # a multimodal content block in the user message below.
    #
    # Phase 1b (2026-08-31): also re-inject files uploaded in EARLIER
    # turns. The frontend only sends file_urls for the CURRENT turn, so a
    # follow-up ("tell me more about that file") would otherwise lose the
    # file content — the agent would claim it "can't re-read" the upload.
    # file_urls are persisted on each user message; re-scan conv.messages
    # (deduped against the current turn) so uploads stay readable for the
    # whole conversation, matching Kimi/GPT.
    try:
        from app.services.synexia.context_assembler import collect_historical_file_urls
        _historical_urls = collect_historical_file_urls(
            getattr(conv, "messages", None) or [],
            exclude=file_urls,
        )
    except Exception as _hist_err:
        logger.debug("v3 stream: historical file_urls scan failed (non-fatal): %s", _hist_err)
        _historical_urls = []
    _all_attachment_urls = list(dict.fromkeys([*file_urls, *_historical_urls]))

    if _all_attachment_urls:
        try:
            from app.services.document_ingestion.service import prepare_for_context
            _attach_parts: list[str] = []
            _image_urls: list[str] = []
            # Chat-upload RAG (2026-08-31): route LARGE files (and
            # many-file turns) through the ChromaDB retrieval pipeline
            # instead of dumping text past the ~30k-token wall. Small
            # single-file turns keep the exact inline behavior. Fail-open:
            # any RAG error degrades back to the plain text dump.
            _rag_available = False
            _upload_rag = None
            if settings.RAG_UPLOADS_ENABLED:
                try:
                    from app.services.document_ingestion import upload_rag as _upload_rag
                    _rag_available = _upload_rag.availability()
                except Exception:
                    _rag_available = False
            _rag_session = conversation_id or "default"
            _rag_org = "default"
            # Full isolation scope: agent + project are stored as Chroma
            # metadata so retrieval filters on the whole stack
            # (org -> agent -> project -> session -> file).
            _rag_agent = agent_name or ""
            _rag_pid = locals().get("_v3_effective_pid") or ""
            _rag_pname = locals().get("_v3_effective_pname") or ""
            _rag_indexed: list[tuple[str, str]] = []
            _rag_any_large = False
            _rag_inlined_small = 0
            _rag_max_files_inline = settings.RAG_UPLOADS_MAX_FILES_INLINE
            _rag_inline_max = settings.RAG_UPLOADS_INLINE_MAX_CHARS
            for _furl in _all_attachment_urls:
                _is_historical = _furl not in file_urls
                _prep = prepare_for_context(_furl)
                _fname = _prep.get("file_name") or _furl
                _origin = " (uploaded earlier)" if _is_historical else ""
                if _prep.get("is_image"):
                    _image_urls.append(_furl)
                    if _prep.get("text"):
                        _attach_parts.append(
                            f"--- {_fname}{_origin} (image, OCR) ---\n{_prep['text']}"
                        )
                    else:
                        _attach_parts.append(
                            f"--- {_fname}{_origin} (image, no OCR text — see multimodal block) ---"
                        )
                elif _prep.get("text"):
                    _text = _prep["text"]
                    _large = len(_text) > _rag_inline_max
                    _many = len(_all_attachment_urls) > _rag_max_files_inline
                    if _rag_available and (_large or _many):
                        # Index once (idempotent per session+file). If the
                        # first extraction was truncated by the per-file
                        # cap, re-extract the FULL text so retrieval can
                        # answer from the whole document.
                        _rag_text = _text
                        if _prep.get("truncated"):
                            try:
                                _full_prep = prepare_for_context(
                                    _furl, max_chars=5_000_000
                                )
                                if _full_prep.get("text"):
                                    _rag_text = _full_prep["text"]
                            except Exception:
                                pass
                        _n = _upload_rag.index_upload_text(
                            _furl, _rag_session, _rag_org,
                            _rag_text, file_name=_fname,
                            agent=_rag_agent,
                            project_id=_rag_pid,
                            project_name=_rag_pname,
                        )
                        if _n and _n > 0:
                            _rag_indexed.append((_furl, _fname))
                            if _large:
                                _rag_any_large = True
                                _attach_parts.append(
                                    f"--- {_fname}{_origin} "
                                    "(large file — relevant passages retrieved below) ---"
                                )
                            elif _rag_inlined_small < _rag_max_files_inline:
                                _attach_parts.append(
                                    f"--- {_fname}{_origin} ---\n{_text}"
                                )
                                _rag_inlined_small += 1
                            else:
                                _attach_parts.append(
                                    f"--- {_fname}{_origin} "
                                    "(passages retrieved below) ---"
                                )
                        else:
                            # Index failed — fall back to the plain dump.
                            _attach_parts.append(
                                f"--- {_fname}{_origin} ---\n{_text}"
                            )
                    else:
                        _attach_parts.append(
                            f"--- {_fname}{_origin} ---\n{_text}"
                        )
                elif _prep.get("error"):
                    _attach_parts.append(
                        f"--- {_fname}{_origin} [could not read: {_prep['error']}] ---"
                    )
            # Retrieval block: top-k chunks for THIS turn's question across
            # every indexed upload in the session.
            if _rag_indexed and (_rag_any_large or len(_all_attachment_urls) > _rag_max_files_inline):
                try:
                    _chunks = _upload_rag.retrieve_upload_chunks(
                        _rag_session, _rag_org, user_content,
                        agent=_rag_agent,
                        project_id=_rag_pid,
                    )
                    _rag_block = _upload_rag.build_retrieval_block(
                        user_content, _chunks
                    )
                    if _rag_block:
                        _attach_parts.append(_rag_block)
                except Exception as _rag_err:
                    logger.debug(
                        "v3 stream: upload retrieval failed (non-fatal): %s",
                        _rag_err,
                    )
            if _attach_parts:
                system_prompt += (
                    "\n\n=== User attachments ===\n"
                    + "\n\n".join(_attach_parts)
                    + "\n\nSome of the file(s) above were uploaded in earlier "
                    "messages of this conversation and remain available for "
                    "follow-up questions. Answer the user's question using "
                    "their content. Quote the file name when citing a passage.\n"
                )
        except Exception as _attach_err:
            logger.warning(
                "v3 stream: attachment injection failed (non-fatal): %s",
                _attach_err,
            )

    # Skill methodology injection
    if agent_app and getattr(agent_app, "skills", None):
        try:
            from app.services.skills_loader import get_skill_prompt_for_agent
            skill_prompt = get_skill_prompt_for_agent(agent_app.skills, db=db)
            if skill_prompt:
                system_prompt += f"\n\n{skill_prompt}"
        except Exception:
            pass

    # Full skill catalog context injection (dynamic discovery + routed
    # skill) — same composition as the main chat path above. Best-effort.
    if agent_app:
        try:
            from app.services.skill_routing.runtime_catalog import (
                build_skill_catalog_context,
            )
            _explicit_skill_name_v3 = None
            if isinstance(selected_skill, dict):
                _explicit_skill_name_v3 = selected_skill.get("name")
            elif isinstance(selected_skill, str):
                _explicit_skill_name_v3 = selected_skill
            _skill_ctx_block_v3 = build_skill_catalog_context(
                user_content,
                db,
                bound_skills=set(agent_app.skills or []),
                context_window_tokens=(
                    effective_llm.endpoint.context_window
                    if effective_llm.endpoint else None
                ),
                explicit_skill_name=_explicit_skill_name_v3,
            )
            if _skill_ctx_block_v3:
                system_prompt += _skill_ctx_block_v3
        except Exception:
            pass

    # FIX 2026-08-23: inject the user-selected skill's SKILL.md body
    # as a hard runtime directive (same pattern as v2 path above).
    if selected_skill_id:
        _sel_skill_block_v3 = _build_selected_skill_runtime_block(
            db, selected_skill, selected_skill_id,
        )
        if _sel_skill_block_v3:
            system_prompt += "\n\n" + _sel_skill_block_v3
            logger.info(
                "v3 selected_runtime_skill injected for conv=%s, skill_id=%s",
                conversation_id, selected_skill_id,
            )

    tools = get_tools(agent_name, tool_config, agent_app)

    # Reuse the pre-computed data-source runtime from above (avoids a
    # duplicate DB query). `prepare_data_source_runtime` is purely a
    # function of the agent + the selected project, so the
    # `bound_kb_ids` set is identical to the one the FSM path got.
    # The `_v3_effective_pid` / `_v3_effective_pname` variables were
    # resolved up-front (body project context takes precedence over
    # conv.project_id) — use them here so the tools list gets the
    # same project-scope the FSM already used.
    try:
        from app.services.data_source_runtime import prepare_data_source_runtime
        from app.services.compaction import get_context_window
        tools, system_prompt, _ = prepare_data_source_runtime(
            db, agent_app, tools, system_prompt,
            selected_project_id=_v3_effective_pid,
            selected_project_name=_v3_effective_pname,
            user_id=user.id if user else None,
            user_message=user_content,
            # P1-5: pass the user-selected model's context window so the
            # Bound Data Sources block auto-compresses for small models.
            # Uses the REAL resolved window (admin-set or auto-probed) so
            # ANY model gets the right budget, not just heuristic names.
            target_context_window=get_context_window(
                user_model or get_model(),
                context_window_tokens=(
                    effective_llm.endpoint.context_window
                    if effective_llm.endpoint else None
                ),
            ),
        )
    except Exception as e:
        logger.debug("Data source runtime prep failed (non-fatal): %s", e)
    data_ctx_extras = _v3_data_ctx_extras or {}

    # Dynamic tool loading (2026-08-31): keep the always-on core + the
    # intent-relevant periphery tools for THIS turn. Cuts ~10-20k tokens of
    # tool schemas per turn. Fail-open: any error returns the full list.
    try:
        from app.services.dynamic_tools import select_tools_for_turn

        tools = select_tools_for_turn(tools, user_content)
    except Exception as _dt_err:
        logger.debug("v3 dynamic tool loading skipped (non-fatal): %s", _dt_err)

    # Concise-mode rule for data turns (2026-08-21): the model narrated the
    # SQL it planned to run and pasted raw results into the final answer —
    # long latency + internal artifacts in the bubble. Demand tool-first
    # behavior and a clean synthesized answer.
    try:
        _tool_names = {
            (t.get("function") or {}).get("name")
            for t in (tools or [])
            if isinstance(t, dict)
        }
    except Exception:
        _tool_names = set()
    if _tool_names & DATA_PRODUCING_TOOLS:
        system_prompt += (
            "\n\n## RESPONSE DISCIPLINE (data turns)\n"
            "- Call the data tool FIRST. Do NOT narrate the SQL you plan to "
            "run, and do NOT write out query text or column lists in prose.\n"
            "- When you call `ask_data_agent`, include the `[schema: ...]` "
            "block from the Bound Data Sources section in your question, and "
            "keep the question SHORT (1-2 sentences). Do NOT paste raw SQL "
            "or join hints — the Data Agent writes its own SQL.\n"
            "- Do NOT paste raw query output (raw IDs, wide tables, JSON) "
            "into your final answer. The platform renders data cards "
            "automatically.\n"
            "- Do NOT include a 'JSON Report Card' section or any raw JSON "
            "payload in the final answer.\n"
            "- After the data returns, write an EXTENSIVE prose analysis "
            "AROUND the data card: an executive summary (3-5 sentences), "
            "key numbers with interpretation, trends and comparisons, "
            "notable anomalies, and 1-3 actionable recommendations. The "
            "user is paying for insight, not just a table.\n"
            "- NEVER respond with only filler text like 'Analyzing N rows…' "
            "or 'Here is the data…'. Every data turn MUST produce a real "
            "written analysis paragraph (at least 4-6 sentences of prose).\n"
        )
        # 2026-08-26: when the user explicitly asks for a written REPORT
        # (review, dashboard, monthly/weekly report, performance, etc.),
        # the analysis must be COMPREHENSIVE — not a brief summary.
        # Triggered by report-style keywords in the user message.
        _user_msg_text = ""
        try:
            for _m in (messages or []):
                if isinstance(_m, dict) and _m.get("role") == "user":
                    _c = _m.get("content")
                    if isinstance(_c, str):
                        _user_msg_text += " " + _c
        except Exception:
            _user_msg_text = ""
        try:
            from app.services.goal_contract import is_report_request
            if is_report_request(_user_msg_text):
                system_prompt += (
                    "\n\n## RESPONSE DISCIPLINE (report request — STRICT)\n"
                    "The user has explicitly asked for a written report "
                    "(review, performance, dashboard, monthly/weekly "
                    "report, KPI/metrics, ranking, etc.). You MUST "
                    "produce a comprehensive written report. Structure:\n"
                    "  1. **Executive Summary** — 4-6 sentence overview of "
                    "the headline finding (what happened, why it matters).\n"
                    "  2. **Key Numbers** — 5-10 concrete figures with "
                    "their meaning (totals, comparisons to prior period, "
                    "top/bottom performers).\n"
                    "  3. **Trends & Comparisons** — patterns over time, "
                    "vs. last month/quarter, vs. plan or benchmark.\n"
                    "  4. **Notable Anomalies** — outliers, surprises, "
                    "items needing attention.\n"
                    "  5. **Recommendations** — 3-5 concrete next actions "
                    "the user should take.\n"
                    "Aim for 400-800 words of prose around the data card. "
                    "Do NOT emit only the data card + a 1-line summary. "
                    "Do NOT say 'Analyzing N rows…' — write the actual "
                    "analysis. Markdown headings and bullet lists are "
                    "welcome.\n"
                )
        except Exception:
            pass

    # ── T17: SESSION STATE block (fail-open, best-effort) ──────────────
    # Append cached last-data-execution metadata before llm_messages freeze
    # so re-export/re-format requests can reuse the execution_id instead of
    # re-running data tools. build_session_state_block returns None when
    # there is no cached row, so this is a no-op when the cache is empty.
    try:
        state_block = build_session_state_block(db, session_id=conversation_id)
        if state_block:
            system_prompt = system_prompt + state_block
    except Exception as _exc:
        logger.debug("[session-state] injection failed: %s", _exc)

    # ── P1-4: delegation nudge (2026-08-29) ─────────────────────────────
    # Parallelizable asks (>=2 top-N/list/comparison clauses) get a one-shot
    # directive to fan out via delegate_task instead of answering linearly.
    # Mirrors the T15 directive pattern above. Dashboard turns excluded
    # (they have their own build-tool forcing).
    try:
        from app.services.delegation_nudge import delegation_nudge_directive

        _del_directive = delegation_nudge_directive(user_content)
        if _del_directive:
            system_prompt += _del_directive
    except Exception as _del_err:  # noqa: BLE001 — non-fatal
        logger.debug("delegation nudge injection failed (non-fatal): %s", _del_err)

    llm_messages = _rebuild_v3_history_messages(system_prompt, messages)

    # Phase 2: multimodal image pass-through. If the current user message
    # carried image attachments, convert the last user message's content
    # into a list of OpenAI-compatible content blocks (text + image_url)
    # so gpt-4o / claude-sonnet / similar vision models see the image
    # natively (in addition to any OCR text injected into the system
    # prompt above). Non-image uploads are unaffected — their text is
    # already in the system prompt and needs no content-block form.
    if file_urls:
        try:
            from app.services.document_ingestion.service import build_image_content_blocks
            _img_blocks = build_image_content_blocks(file_urls)
            if _img_blocks and llm_messages and llm_messages[-1].get("role") == "user":
                _last = llm_messages[-1]
                _text = str(_last.get("content") or "")
                _last["content"] = [{"type": "text", "text": _text}] + _img_blocks
        except Exception as _img_err:
            logger.warning(
                "v3 stream: image content block injection failed (non-fatal): %s",
                _img_err,
            )

    tool_calls_for_frontend: list[dict] = []
    guardrail_retries = 0
    # Phase 1: one reactive-compaction pass per turn on stream context
    # overflow (see the stream error handler). NOTE: must be a mutable list
    # so the nested event_stream generator can flip it without declaring
    # nonlocal — assigning the bare name inside event_stream() would make
    # it local to event_stream, turning every earlier read into
    # UnboundLocalError and killing the SSE stream (same trap as
    # llm_messages, guardrail_retries, etc.).
    _stream_compaction_attempted = [False]
    assistant_content = ""
    # Fix 2a: assistant_content length at the last ask_data_agent result, so
    # the text BETWEEN two ask_data_agent calls can be checked for retry
    # hints (an explicit re-query supersedes the earlier result).
    _ask_data_content_len = [0]
    # Smart-retry budget (LLM-driven broadening) for empty ask_data_agent
    # results with a file-format intent. Mutable list so the nested
    # generator can decrement it without declaring nonlocal.
    _smart_retry_budget = [2]
    # Set True when the no-data branch renders a card-only outcome (no
    # empty PPTX/DOCX). The post-loop artifact fallback must be skipped
    # for that turn — an empty file is worse than a clear no-data card.
    _orch_no_data = [False]

    async def event_stream():
        nonlocal assistant_content, guardrail_retries, messages, conv

        # T18: mark this turn as dashboard-intent so the artifact persistence
        # layer can drop stray analytics-path artifacts (e.g. a static "Web
        # page" written from the agent's narration sentence) that would land on
        # the same thread as the real dashboard app. Reset in the finally below
        # so the flag never leaks across turns.
        _dash_intent_token = set_dashboard_intent(bool(_is_dashboard_request(user_content)))

        # CRITICAL: FastAPI's ``get_db`` dependency closes the injected
        # ``db`` session in its ``finally`` block as soon as this endpoint
        # *returns* the ``StreamingResponse``. That happens BEFORE the
        # generator below is consumed — so by the time we reach the
        # tool-calling loop, ``db.is_active`` is already ``False`` and
        # ``conv`` is detached. Every subsequent ``db.commit()`` would
        # silently no-op (the symptom: streamed text appears via SSE but
        # never lands in the DB → "vanishing text" on reload).
        #
        # Fix: open a dedicated session for the lifetime of the
        # generator and shadow the closed ``db`` parameter. The session
        # is closed by the generator's own ``finally`` at the very end.
        from app.database import SessionLocal as _StreamSessionLocal
        stream_db = _StreamSessionLocal()
        db = stream_db  # noqa: F811 — intentional shadow of the closed dep
        _fresh_conv = db.query(AgentConversation).filter(
            AgentConversation.id == conversation_id,
            AgentConversation.is_deleted == False,
        ).first()
        if _fresh_conv is not None:
            conv = _fresh_conv
            messages = list(conv.messages or [])

        assistant_msg_id = str(uuid.uuid4())
        artifact_ids: list[str] = []
        # File-format intent is needed INSIDE the loop (synthesis gate for
        # the empty-rows path) as well as after it (orchestrator fallback),
        # so compute it once up front.
        _orch_doc_format = detect_file_intent(user_content)
        # Dashboard builds (2026-08-27): a full-stack dashboard turn is a LONG
        # multi-phase pipeline (design system → schema → data collection →
        # build → verify). It gets its own budget below — the fast-mode caps
        # (10 iterations / 180s wall clock) fired mid-pipeline and the user
        # got the "turn ended" note instead of the live app.
        _is_dashboard_build = bool(_orch_doc_format == "dashboard")

        # ── Activity steps tracking (Claude-style inline numbered steps) ──
        _step_counter = [0]  # mutable counter (list for nonlocal access)
        # Turn-start timestamp for the agent_invocations recorder (P1, 2026-08-29).
        from datetime import datetime as _dt, timezone as _tz

        _turn_started = _dt.now(_tz.utc)
        # 2026-08-27: sink for opt-in ask_data_agent sub-step SSE frames.
        _tool_progress: list[str] = []
        _activity_steps: list[dict] = []  # accumulate for final persistence
        _emitted_phases: set[str] = {"goal"}  # phase headlines already sent this turn
        reasoning_acc: list[str] = []  # P0: accumulate reasoning across iterations for final assistant_msg

        # ── Typed live-activity feed (inline per-message stream) ──
        # Parallel to the legacy numbered steps: structured events
        # {type, label_key, params, ts} surfaced to the frontend's
        # LiveActivityStream and persisted on the assistant message.
        _live_events: list[dict] = []
        _live_event_count = [0]

        def _push_live_event(event_type: str, label_key: str, params: dict | None = None, sanitize: bool = True) -> str | None:
            """Build a live event, record for persistence, return its SSE frame."""
            _ev = _build_live_event(event_type, label_key, params, count=_live_event_count, sanitize=sanitize)
            if _ev is not None:
                _live_events.append(_ev)
                return _sse_live_event(_ev)
            return None

        # Claude-style phase headline: the turn always opens in "Fathoming".
        yield _emit_phase("goal")
        # NOTE: the typed live feed deliberately does NOT emit phase_enter.goal
        # here. The feed only materializes once the turn proves tool-bound
        # (first tool batch) — chitchat turns that never call a tool render no
        # activity box at all. The goal event is fired alongside the first
        # act batch below.

        # Emit "Understanding your request" step (running → done)
        _step_counter[0] += 1
        _step_start_times: dict[int, float] = {}  # step_num → monotonic start time
        _step_start_times[_step_counter[0]] = time.monotonic()
        yield _emit_activity_step(
            _step_counter[0], "Understanding your request", "running",
        )
        _activity_steps.append({
            "number": _step_counter[0],
            "description": "Understanding your request",
            "status": "running",
        })
        _step_dur = int((time.monotonic() - _step_start_times[_step_counter[0]]) * 1000)
        yield _emit_activity_step(
            _step_counter[0], "Understanding your request", "done",
            duration_ms=_step_dur,
        )
        _activity_steps[-1]["status"] = "done"
        _activity_steps[-1]["duration_ms"] = _step_dur

        # P0 reliability: per-turn guardrail controller + per-conversation iteration budget
        guard_ctrl = ToolLoopGuardController(_loop_guard_config_for(agent_app))
        _max_iters = getattr(agent_app, "max_call_count", None) or settings.AGENT_MAX_ITERATIONS
        conv_budget = IterationBudget(max_total=_max_iters)
        _verify_attempts = 0
        _gate_attempts = 0  # universal self-eval re-plan nudges this turn
        _cad_verify_nudges = 0  # CAD verify-on-stop nudges this turn (cap 1)
        _fusion_clear_count = 0  # fusion360_clear calls this turn (cap 1 — see _invoke_v3)
        _fusion_readonly_streak = 0  # consecutive read-only fusion calls (info/ping) — spin guard
        # Clarify handoff (2026-08-28): when the model issues a clarify
        # question, the turn must END — the user's next message is the
        # answer (CLARIFY_SUSPENDS_TURN_ENABLED). Without this, the loop
        # kept iterating after clarify and burned the remaining budget on
        # guard blocks / failing tools, ending in a confusing
        # "phase_enter.act failed" + verify_failed instead of a clean pause
        # (Sales Performance Dashboard turn, conv f62e4c2b).
        _clarify_issued = False
        _clarify_question_text = ""
        # Batch-replay guard state: fingerprint (sorted tool name+args JSON) →
        # count within THIS turn. A large identical batch re-emitted N times
        # escapes the per-tool loop guard (each tool appears once per batch),
        # so we fingerprint whole batches and break at >= 3 repeats.
        _batch_fp_counts: dict[str, int] = {}
        _pptx_nudge_attempts = 0  # pptx turn-guard nudges this turn
        # Fix 1b: one-shot "force create_artifact next iteration" signal set
        # by the LAST allowed pptx nudge (pptx_turn_guard.force_next). Read at
        # the top of the next iteration next to should_force_create_pptx, so
        # prose deflection can never end the turn without a deck.
        _pptx_force_next_iteration = False
        # File-deliverable turn guard: mirrors pptx guard for html/docx/pdf/
        # xlsx/md.  Detects format from user prompt OR automation metadata.
        _file_nudge_attempts = 0
        _file_force_next_iteration = False
        _output_format = body.get("output_format")  # automation runtime may pass
        # Initialize BEFORE the loop: the budget guard and the tool-call
        # loop guard can both ``break`` on the FIRST iteration, before the
        # in-loop assignment runs — the post-loop ``if not content_streamed``
        # read then raised UnboundLocalError and killed the SSE stream
        # ("Sorry, the connection was interrupted" in the chat UI).
        content_streamed = False  # tracks whether deltas were sent live
        # Turn-scope marker for the loop guard: only tool calls made during
        # THIS turn (from the triggering user message onward) count toward
        # the hard cap. Scanning the whole history made legitimate
        # cross-turn repetitions (e.g. re-running the same automation via
        # "Run Now" every day) trip the in-turn guard on iteration 0.
        _turn_start_idx = max(0, len(llm_messages) - 1)
        # Phase 3: dynamic tool budget — estimate how many ask_data_agent
        # calls this query needs based on concept/metric count.
        _dynamic_tool_caps: dict[str, int] = {}
        if user_content:
            _data_cap = _estimate_ask_data_agent_cap(user_content)
            if _data_cap > TOOL_CALL_CAPS.get("ask_data_agent", 2):
                _dynamic_tool_caps["ask_data_agent"] = _data_cap
                logger.info(
                    "Dynamic tool cap: ask_data_agent=%d (query complexity from '%s…')",
                    _data_cap,
                    user_content[:60],
                )
            # fetch_data_batch gets a proportional cap (each call can contain
            # multiple parallel queries, so fewer calls needed)
            _fetch_cap = max(2, _data_cap // 2)
            if _fetch_cap > TOOL_CALL_CAPS.get("fetch_data_batch", 3):
                _dynamic_tool_caps["fetch_data_batch"] = _fetch_cap
            # Dashboard builds (2026-08-27): schema exploration alone is
            # several fetch_data_batch calls (SHOW TABLES, DESCRIBE per
            # table, SHOW COLUMNS) BEFORE any data pull. The generic cap
            # (3 calls, name-only keyed) tripped the tool-call loop guard
            # mid-exploration on conv 3e7fa92b — the loop broke before
            # create_fullstack_dashboard could ever be called, and the
            # user got a "readiness assessment" report instead of the app.
            # On dashboard turns, give data tools the dashboard exploration
            # budget (the exploration cap itself stays authoritative for
            # steering the model to build).
            if _is_dashboard_build:
                _dash_data_cap = max(
                    getattr(settings, "MAX_DASHBOARD_EXPLORATION_PER_TURN", 8),
                    _dynamic_tool_caps.get("fetch_data_batch", 0),
                    _dynamic_tool_caps.get("ask_data_agent", 0),
                )
                _dynamic_tool_caps["fetch_data_batch"] = max(
                    _dynamic_tool_caps.get("fetch_data_batch", 0), _dash_data_cap,
                )
                _dynamic_tool_caps["ask_data_agent"] = max(
                    _dynamic_tool_caps.get("ask_data_agent", 0), _dash_data_cap,
                )
                logger.info(
                    "Dashboard turn: data-tool caps raised to %d (fetch_data_batch/ask_data_agent)",
                    _dash_data_cap,
                )
        # Finish-line state (UnboundLocalError discipline: initialized BEFORE
        # the loop). ``dashboard_forced`` tracks whether the dashboard guard
        # forced ``create_dashboard`` this turn; ``_wrapup_nudged`` ensures
        # the T-3 wrap-up message is injected exactly once.
        dashboard_forced = False
        _wrapup_nudged = False
        # ── Dynamic per-turn budget (schema-aware soft cap) ───────────
        # db-bound agents get a dynamic iteration budget that scales with
        # schema-graph join complexity (see calculate_agent_budget in
        # agent_prompts). MAX_TOOL_ITERATIONS stays the absolute hard cap;
        # this soft cap can be RAISED mid-loop once describe_schema reveals
        # join edges (see the upgrade hook in the tool-execution branch).
        _auto_flag = bool(
            body.get("force_planning")
            or body.get("phase") == "automation"
            or _auto_task_id
            or _auto_exec_id
        )
        _effective_budget = calculate_agent_budget(
            None, user_content or "", is_automation=_auto_flag,
        )
        if _is_dashboard_build:
            # Dashboard builds are 15-30+ tool calls; the dynamic soft cap
            # (max 10) would break the loop before the build step. Use the
            # dashboard budget (40) so the pipeline runs to completion.
            _effective_budget = max(_effective_budget, settings.DASHBOARD_BUILD_MAX_TOOL_ITERATIONS)
        _schema_edges_seen = False
        # T12: per-turn describe_schema cap (dashboard turns). The agent can
        # burn its whole tool-loop budget re-inspecting the schema instead of
        # shipping the dashboard; once the cap fires we block further
        # describe_schema calls, freeze the budget-upgrade hook (so the soft
        # cap stops widening), and nudge the model to build NOW.
        _describe_schema_count = 0
        _schema_budget_frozen = False
        _v3_executed_tool_names: list[str] = []
        # Tier 1 auto-refine tracking (2026-08-28): remember the dashboard
        # build's quality grade + gaps and whether an update followed, so the
        # done frame can warn when a B/C board was shipped without refinement.
        _dash_quality_worst: str | None = None
        _dash_quality_gaps: list[str] = []
        _dash_refined: bool = False
        # P1-4b: hard delegation nudge — injected ONCE per turn when the user
        # asked for parallel work but the model keeps answering linearly.
        _delegation_nudged: bool = False
        _delegation_enforced: bool = False
        # T19: tools that were CALLED this turn but FAILED (returned
        # {"success": False} or threw). Lets the dashboard-orchestrator guard
        # distinguish "build tool ran OK" from "build tool crashed" — a failed
        # build must NOT unlock the static-artifact fallback.
        _v3_failed_tool_names: set[str] = set()
        # Fix 4: titles of create_artifact calls already executed this turn,
        # so a second identical static page is blocked (always waste).
        _create_artifact_titles: set[str] = set()
        # Fix 6: narration-nudge guard counter. The model sometimes exits the
        # v3 loop with ONLY narration (no tool call) on a dashboard-shaped
        # turn; each nudge forces the EXACT next workflow step and continues
        # the loop. Capped by MAX_DASHBOARD_NARRATION_NUDGES so a weak model
        # cannot be nagged forever.
        _dashboard_narration_nudges: int = 0
        # ── Goal-Contract closed loop (flag-gated) ────────────────────
        # A machine-checkable contract for this turn: what deliverable (if
        # any) the user asked for, whether data is required, and whether the
        # model promised a follow-up tool call in prose. Built ONCE per turn
        # here, updated at runtime from tool results, and checked FIRST in
        # the no-tool-calls exit branch. When GOAL_CONTRACT_ENABLED is off,
        # ``_contract`` stays None and every legacy guard below runs
        # bit-for-bit as before (flag-off = current behavior).
        _contract = None
        _contract_force_tool = None  # remediation force armed by the exit checker
        _contract_force_synthesis = False  # same, but forces tool_choice="none" for synthesis
        _empty_answer_forces = 0          # Fix 2: cap of one re-synthesis per turn
        _empty_answer_force_next = False  # Fix 2: armed by the exit-branch net
        _last_iter_prose = ""            # Fix 3: last iter's prose for promise-as-answer check
        if settings.GOAL_CONTRACT_ENABLED:
            try:
                _bound_kb_ids = data_ctx_extras.get("bound_kb_ids") or []
                _contract = build_goal_contract(
                    user_content or "",
                    agent_config={
                        "tools": _tool_names_from_schemas(tools),
                        "bound_kb_ids": _bound_kb_ids,
                    },
                    max_forces=settings.GOAL_CONTRACT_MAX_FORCES,
                    table_executor=_make_goal_contract_table_executor(db, _bound_kb_ids),
                )
            except Exception as _gc_err:  # noqa: BLE001 — contract must never break the loop
                logger.warning("goal-contract build failed (fallback to legacy): %s", _gc_err)
                _contract = None
        # ── Turn plan (2026-08-27): deterministic plan-first ──────────
        # The agent MUST make a plan (todo list) from the user's input BEFORE
        # acting, then follow it. Previously planning was optional model prose
        # that the local LLM rarely produced — the loop freewheeled into
        # repeated ask_data_agent calls and shipped no deliverable. The plan
        # is derived deterministically from intent, emitted as plan_step_added
        # events (live checklist in the UI), and injected into the model
        # context so the loop follows it. Flag-gated: TURN_PLAN_ENABLED.
        _turn_plan = None
        _turn_plan_completed: set[int] = set()
        if settings.TURN_PLAN_ENABLED:
            try:
                _turn_plan = build_turn_plan(
                    user_content or "",
                    is_dashboard_build=_is_dashboard_build,
                    is_report_request=bool(_orch_doc_format),
                    tool_names=_tool_names_from_schemas(tools),
                )
                # Dynamic per-request plan (2026-08-27): one cheap LLM
                # planning call tailors the steps to THIS request (the exact
                # metrics / regions / tools the user named), not a generic
                # template. Only for deliverable/data kinds — generic turns
                # keep the fixed template with zero added latency. Falls back
                # to the fixed plan on any failure (timeout, invalid JSON,
                # too few/many steps) — the deterministic backbone always
                # stands.
                if (
                    _turn_plan is not None
                    and _turn_plan.kind in ("dashboard", "report", "data")
                    and settings.TURN_PLAN_DYNAMIC_ENABLED
                ):
                    _dyn_plan = await _generate_dynamic_turn_plan(
                        user_content or "",
                        _turn_plan.kind,
                        _tool_names_from_schemas(tools),
                        endpoint=effective_llm.endpoint,
                    )
                    if _dyn_plan is not None:
                        _turn_plan = _dyn_plan
                if _turn_plan and _turn_plan.steps:
                    # Step 1 (analyze/understand) is complete by definition:
                    # the request was read to build the plan.
                    _turn_plan_completed = {
                        s.step_index for s in _turn_plan.steps if s.key == "analyze"
                    }
                    if "plan" not in _emitted_phases:
                        _emitted_phases.add("plan")
                        yield _emit_phase("plan")
                        # Persist the plan phase as a typed live event so the
                        # reloaded message shows the same "Laying out the plan"
                        # headline the live stream showed (previously the plan
                        # phase was raw-SSE only and vanished on reload).
                        _le_plan_phase = _push_live_event("phase_enter", "phase_enter.plan")
                        if _le_plan_phase:
                            yield _le_plan_phase
                    for _st in _turn_plan.steps:
                        yield plan_step_added_frame(_st)
                        # Persist each plan step as a typed live event so the
                        # plan checklist survives reload. Titles are
                        # server-generated plan metadata (same class as tool
                        # labels) — bypass the SQL/ERP content scrubber so
                        # "Query the bound data source" renders verbatim.
                        _le_step = _push_live_event(
                            "plan_step_added", "plan_step_added",
                            {"step_index": _st.step_index, "title": _st.title_en},
                            sanitize=False,
                        )
                        if _le_step:
                            yield _le_step
                    llm_messages.append({
                        "role": "user",
                        "content": plan_to_system_block(_turn_plan),
                    })
            except Exception as _plan_err:  # noqa: BLE001 — planning must never break the loop
                logger.warning("turn-plan build failed (planless loop): %s", _plan_err)
                _turn_plan = None
        # ── Query-purpose resolver (flag-gated, Bug 2 fix) ─────────────
        # Resolves catalog table roles ONCE per turn (memoized) so every
        # ask_data_agent result can be tagged probe/auxiliary/answer before
        # it may feed the deliverable. Fail-open: on any init error the
        # resolver stays None and classification degrades to shape-only.
        _purpose_resolver = None
        if settings.QUERY_PURPOSE_TAGGING_ENABLED:
            try:
                _purpose_resolver = TableRoleResolver(
                    db,
                    kb_ids=(data_ctx_extras or {}).get("bound_kb_ids") or [],
                    project_id=_v3_effective_pid,
                )
            except Exception as _purpose_init_err:  # noqa: BLE001
                logger.warning(
                    "query-purpose resolver init failed (shape-only fallback): %s",
                    _purpose_init_err,
                )
                _purpose_resolver = None
        _loop_exit_monotonic = None
        # Multi-iter content accumulator (2026-08-20):
        # The v3 loop resets `assistant_content = ""` at the top of every
        # iteration. Without this accumulator, the persisted assistant_msg
        # and the `done` event's content field end up carrying ONLY the
        # LAST iteration's prose — all earlier iterations' content (tables,
        # key findings, recommendations) is silently lost. We capture each
        # iter's content BEFORE the reset and join them post-loop.
        _v3_iter_contents: list[str] = []
        # Recovery buffer: when a nudge site pops good content from the
        # accumulator, we save the longest popped entry here. If the loop
        # exits with empty/stale content (nudge failed, max iterations),
        # we recover this best prior iteration instead of falling back to
        # generic "I had trouble..." or raw data rows. (Bug 2026-08-23)
        # Wrapped in a list so nested event_stream can mutate without
        # nonlocal (same pattern as _stream_compaction_attempted).
        _v3_recovered_best = [None]  # type: ignore[var-annotated]
        accumulated_content: str = ""  # post-loop computed; safe default for early-exit paths
        # D1/D2 (2026-08-20): once a gate nudges a re-synthesis, every
        # subsequent streamed delta must REPLACE the bubble (content_replace
        # SSE) instead of appending — otherwise the nudge-reply prose leaks
        # into the final user-facing answer. Turn-scoped: once True it stays
        # True for the rest of the turn (all post-nudge iterations are
        # replacement content, only the last synthesis survives).
        _nudge_replacement_pending = False
        # UX FIX (2026-08-24): When the user asked for a file deliverable
        # (docx, pptx, xlsx, pdf, html, md), suppress ALL intermediate text
        # in the chat bubble until the turn is fully complete. Without this
        # the user sees streaming analysis that gets replaced/collapsed by
        # the final synthesis — jarring "make-then-collapse" UX. We only
        # show the FINAL accumulated_content in the very last SSE event.
        _suppress_chat_deltas = bool(_orch_doc_format)
        # 2026-08-27: agent fast mode — 10 iterations (not 40) keeps turn
        # time under 60s for ALL models (deepseek, qwen, etc.)
        _v3_max_iterations = _effective_max_tool_iterations(effective_llm)
        if _is_dashboard_build:
            # Dashboard builds are 15-30+ tool calls (design → schema → data
            # → build → verify). Lift the fast-mode 10-iteration cap to the
            # dashboard budget (40). An explicit agent_app.max_iterations
            # below still wins — the user's per-agent choice is respected.
            _v3_max_iterations = max(_v3_max_iterations, settings.DASHBOARD_BUILD_MAX_TOOL_ITERATIONS)
        # Per-agent override (2026-08-27): AgentApp.max_iterations beats the
        # fast-mode default. The CAD Agent seeds 30 because a Fusion build is
        # a 9-15+ step sequential tool loop (sketch→extrude→fillet→verify),
        # which would otherwise be cut off at 10 iterations.
        if agent_app and isinstance(getattr(agent_app, "max_iterations", None), int) and agent_app.max_iterations > 0:
            _v3_max_iterations = agent_app.max_iterations
        # 2026-08-25: hard wall-clock cap on the orchestrator loop.
        # 60s keeps the user's HTTP connection alive (typical proxy/browser
        # idle cap is 240s, but observed 228s "connection interrupted"
        # before any message was saved). We log the break so the
        # post-loop synthesis can still produce a response from whatever
        # rows were collected so far.
        import time as _time_outer
        _loop_t0 = _time_outer.monotonic()
        # 2026-08-25: hard wall-clock cap on the orchestrator loop.
        # 60s keeps the user's HTTP connection alive (typical proxy/browser
        # idle cap is 240s, but observed 228s "connection interrupted"
        # before any message was saved).
        # 2026-08-26: deliverable/report requests (docx/pptx/pdf/xlsx) get
        # a LARGER budget (180s) — the ask_data_agent sub-loop alone takes
        # 20-40s on qwen3.6-27b, so a 60s cap fired BEFORE post-loop
        # synthesis could run, and the user got the generic fallback
        # instead of the real report. 180s stays under the 240s proxy
        # ceiling while giving the loop room to collect data AND let
        # synthesis run. Chat-only requests keep the snappy 60s cap.
        # 2026-08-27: CAD/Fusion agents need the same larger budget as report
        # requests — a build is a multi-step tool loop (ping→sketch→extrude→
        # verify, 4-12+ iterations) and on a local LLM at ~20s/iteration the
        # 60s cap fired BEFORE post-loop synthesis, so the user got the
        # generic "trouble putting it all together" fallback instead of the
        # model result. Detect via enabled fusion360_* tools (per-agent).
        _is_fusion_agent = bool(
            tool_config
            and isinstance(tool_config.get("enabled_tools"), list)
            and any(
                isinstance(t, str) and t.startswith("fusion360")
                for t in tool_config.get("enabled_tools")
            )
        )
        # 2026-08-28: CAD/Fusion soft-budget lift. The dynamic soft cap
        # (calculate_agent_budget returns a small base budget like 4 for
        # non-DB messages) breaks the loop at iteration 4 — mid-build — so a
        # Fusion build never finishes (observed: clear→sketch→polygon→extrude
        # then a forced wrap-up, no verify_build). Dashboard builds already get
        # a lift (above); CAD builds get the same treatment: the soft cap is
        # raised to the per-agent hard cap so the build runs until the model
        # calls fusion360_verify_build and produces its final answer. The
        # wall-clock cap (180s for fusion agents) still bounds runaway turns.
        if _is_fusion_agent:
            _effective_budget = max(_effective_budget, _v3_max_iterations)
        _LOOP_WALL_CLOCK_CAP_S = (
            # Dashboard builds (2026-08-27): 30-minute wall clock — effectively
            # no cap. The pipeline is multi-phase and the local LLM takes ~20s
            # per call; the old 180s cap fired mid-data-gathering and the user
            # got the canned "turn ended" note instead of the live app.
            # Long-running build tools emit progress frames, so the SSE
            # connection stays alive for the full build.
            settings.DASHBOARD_BUILD_WALL_CLOCK_CAP_S
            if _is_dashboard_build
            else (180.0 if (_orch_doc_format or _is_fusion_agent) else 60.0)
        )
        for iteration in range(_v3_max_iterations):
            # 2026-08-25: wall-clock cap. If we've been looping >60s, break
            # and let the post-loop synthesizer produce a final answer from
            # whatever we have. Prevents the "228s connection interrupted"
            # failure mode.
            _now = _time_outer.monotonic()
            if _now - _loop_t0 > _LOOP_WALL_CLOCK_CAP_S:
                logger.warning(
                    "v3 stream: wall-clock cap %.0fs hit on conv %s (iter=%d) — "
                    "breaking loop to let post-loop synthesis save the response",
                    _LOOP_WALL_CLOCK_CAP_S, conversation_id, iteration,
                )
                break
            # P0: consume one iteration from the conversation-level budget
            if not conv_budget.consume():
                logger.info(
                    "Conversation %s iteration budget exhausted (%d/%d), breaking",
                    conversation_id, conv_budget.used, conv_budget.max_total,
                )
                break
            # ── Dynamic soft-cap check ────────────────────────────────
            # The per-turn budget is a soft cap: the model self-regulates
            # via the T-3 wrap-up nudge below, and we break here if it
            # keeps going. MAX_TOOL_ITERATIONS still bounds the range.
            if iteration >= _effective_budget:
                # Goal-Contract soft-cap suppression: when the turn contract
                # still has unmet criteria (deliverable unproduced / zero-row
                # re-query / announced tool), skip the soft-cap break so the
                # exit-checker force machinery below can fire. The hard cap
                # MAX_TOOL_ITERATIONS still bounds the loop.
                _gc_soft_continue = False
                if settings.GOAL_CONTRACT_ENABLED and _contract is not None:
                    # Fix 3a: update pending action from last iter's prose
                    # BEFORE checking unmet, so the sequence-stamp state is
                    # current even when the model produced promise text
                    # alongside tool calls (the exit branch only calls
                    # refresh_pending_action on text-only responses).
                    if _last_iter_prose:
                        _contract.refresh_pending_action(_last_iter_prose)
                    if bool(_contract.unmet(_tool_names_from_schemas(tools))):
                        _gc_soft_continue = True
                    else:
                        # Fix 3b: promise-as-answer gap — the model produced
                        # promise text ("Let me verify…") alongside tool calls
                        # in a prior iteration. refresh_pending_action arms the
                        # pending action, but record_tool_executed (called
                        # during tool execution AFTER arming) advances
                        # _executed_seq past _armed_seq, so _unmet_pending_action
                        # returns [] and unmet() misses it. Direct check
                        # bypasses the sequence-stamp logic.
                        _last_pending = pending_action_phrase(_last_iter_prose)
                        if (
                            _last_pending
                            and _contract._usable_results > 0
                            and _contract.forces_used < _contract.max_forces
                        ):
                            _gc_soft_continue = True
                            _contract.record_force()
                            _contract_force_synthesis = True
                            _nudge_msg = (
                                f"You announced: \"{_last_pending}\" — but you "
                                f"already have the data. Write the final answer now "
                                f"using the retrieved data. Do not announce future actions."
                            )
                            llm_messages.append({"role": "assistant", "content": _last_iter_prose})
                            llm_messages.append({"role": "user", "content": _nudge_msg})
                            # Pop the last iter content so re-synthesis replaces
                            # it instead of appending alongside the promise text.
                            # BUGFIX 2026-08-23: save popped good content so we
                            # can recover it if the nudge fails (max iter / loop
                            # exit with empty content).
                            if _v3_iter_contents:
                                _popped = _v3_iter_contents.pop()
                                if len(_popped or "") > len(_v3_recovered_best[0] or ""):
                                    _v3_recovered_best[0] = _popped
                            assistant_content = ""
                            _nudge_replacement_pending = True
                            logger.info(
                                "v3 stream: promise-as-answer at soft-cap → forcing synthesis "
                                "(conv=%s, iter=%d, phrase=%s, forces=%d/%d)",
                                conversation_id, iteration, _last_pending[:60],
                                _contract.forces_used, _contract.max_forces,
                            )

                if _gc_soft_continue:
                    logger.info(
                        "v3 stream: soft-cap reached but goal-contract unmet (conv=%s, iter=%d); continuing",
                        conversation_id, iteration,
                    )
                else:
                    logger.info(
                        "v3 stream: dynamic per-turn budget reached (%d) for conv=%s; wrapping up",
                        _effective_budget, conversation_id,
                    )
                    break
            # --- Mid-turn steer drain (P2) ---
            # Pick up any user steer messages enqueued by the POST /steer
            # endpoint since the last iteration. Inject them into the LLM
            # message history as user messages and yield a `steer` SSE event
            # so the client can render an inline marker. Best-effort: a
            # steer-bus failure must never break the chat path.
            try:
                _steer_msgs = steer_bus.drain(conversation_id)
            except Exception as _steer_drain_err:
                logger.warning(
                    "v3 event_stream: steer drain failed (non-fatal): %s",
                    _steer_drain_err,
                )
                _steer_msgs = []
            if _steer_msgs:
                for _sm in _steer_msgs:
                    llm_messages.append({"role": "user", "content": _sm})
                yield _emit_steer(_steer_msgs)

            # Per-tool-name runaway guard. If the LLM has called the same
            # tool TOOL_CALL_HARD_CAP times already, inject a final nudge
            # and break — do NOT make another LLM call. Stops the
            # classic skills/skills_hub loop on agent_builder.
            # Scoped to the CURRENT turn: repetitions from earlier turns
            # (e.g. the user re-running the same automation) are legitimate
            # and must not trip the in-turn guard.
            loop_info = _detect_tool_call_loop(llm_messages, start_idx=_turn_start_idx, dynamic_caps=_dynamic_tool_caps)
            if loop_info is not None:
                looped_tool, looped_n = loop_info
                # Internal LLM-facing nudge: tells the model to stop
                # calling the same tool and wrap up. Scaffolding for
                # the model, not for the user.
                nudge = (
                    f"Tool '{looped_tool}' was already called {looped_n} times. "
                    "Use the result you have and produce your final answer. "
                    "Do not call it again."
                )
                # User-facing assistant content: friendly text, no
                # internal tool names or call counts. The previous
                # implementation reused `nudge` for both, leaking
                # internal scaffolding into the chat UI. After R4
                # we explicitly tell the user we are proceeding with
                # sensible defaults so the loop guard stopping the
                # agent does not feel like a dead end.
                #
                # The original message ("I'm going to build the agent
                # with sensible defaults now…") is specific to the
                # ``agent_builder`` flow where the guard was first
                # needed. For every other agent (Sales, Research,
                # general_assistant, …) that wording is nonsensical —
                # the user is NOT building an agent, they're asking a
                # normal question. Use an agent-aware message so the
                # loop guard doesn't feel like a dead end regardless
                # of which agent tripped it.
                if agent_name == "agent_builder":
                    user_facing = (
                        "I'm going to build the agent with sensible defaults now. "
                        "You can adjust anything after creation."
                    )
                else:
                    user_facing = (
                        "I noticed I was repeating the same tool call, so I'll "
                        "stop here and answer based on what I've gathered so far."
                    )
                logger.warning(
                    "Tool-call loop guard tripped in conversation %s: "
                    "tool=%r count=%d (cap=%d). Breaking loop.",
                    conversation_id, looped_tool, looped_n,
                    _dynamic_tool_caps.get(looped_tool)
                    or TOOL_CALL_CAPS.get(looped_tool, TOOL_CALL_HARD_CAP),
                )
                # Bug 2 fix: do NOT stream the reflexion text as the user's
                # response. The previous code set assistant_content =
                # user_facing and yielded it as a delta, which surfaced
                # internal FSM state ("repeating the same tool call") in
                # the chat UI. Instead, break the loop and let the
                # post-loop fallback (see _FALLBACK_EMPTY_LOOP_GUARD_MSG
                # below) produce a clean response if the LLM didn't
                # already accumulate one across the tool iterations.
                llm_messages.append({"role": "user", "content": nudge})
                # Mark that we tripped the loop guard so the post-loop
                # fallback can pick an appropriate user-facing message
                # (the agent_builder-specific copy above vs a generic
                # "I gathered some information" message for other agents).
                _loop_guard_user_facing = user_facing
                break
            # Finish line: with 3 iterations left (per the DYNAMIC soft cap),
            # tell the model to stop exploring and assemble its final answer
            # (injected exactly once).
            if iteration == _effective_budget - 3 and not _wrapup_nudged:
                _wrapup_nudged = True
                llm_messages.append({
                    "role": "user",
                    "content": (
                        "You have 3 steps left. Stop exploring and produce "
                        "your final answer with what you have."
                    ),
                })
            tool_choice = _compute_tool_choice(
                user_content, data_ctx_extras, iteration,
                tool_names=_tool_names_from_schemas(tools),
            )

            # Dashboard guard: if the user asked for a live dashboard and
            # we've already done the schema/design pass, force the next LLM
            # turn to call `create_dashboard` so the dashboard renders inline
            # in the chat. Without this, the agent often keeps running
            # `execute_query` and dumps markdown tables instead of producing
            # an interactive dashboard artifact.
            # Also fires in dashboard-capable projects when the
            # agent speculatively loads dashboard skills before being asked.
            _tool_names = _tool_names_from_schemas(tools)
            _is_dash_project = bool(conv.agent_name and (
                "bi_assistant" in conv.agent_name.lower()
            ))
            _dash_build_tool = dashboard_build_tool()
            if (
                _dash_build_tool in _tool_names
                and should_force_create_dashboard(
                    user_content,
                    tool_calls_for_frontend,
                    has_dashboard_tool=True,
                    is_dashboard_project=_is_dash_project,
                )
            ):
                logger.info(
                    "v3 stream: forcing %s after schema/design "
                    "pass (conv=%s, iter=%d, prior_tool_count=%d, dash_project=%s)",
                    _dash_build_tool, conversation_id, iteration,
                    len(tool_calls_for_frontend), _is_dash_project,
                )
                tool_choice = {
                    "type": "function",
                    "function": {"name": _dash_build_tool},
                }
                dashboard_forced = True
            # PPTX turn-guard: mirror the dashboard forcing for artifact decks.
            # When the user asked for a PPT and the tool-loop budget window is
            # closing with no pptx artifact created, force create_artifact so
            # the model must emit the call (dashboard forcing wins via the
            # dashboard_forced guard inside should_force_create_pptx).
            _pptx_forced = False
            # Fix 1b: the last-allowed synthesis-boundary nudge arms
            # _pptx_force_next_iteration, which overrides the T-window check
            # here so the model MUST emit create_artifact next turn.
            if _pptx_force_next_iteration or should_force_create_pptx(
                user_content,
                tool_calls_for_frontend,
                iteration=iteration,
                max_iterations=MAX_TOOL_ITERATIONS,
                has_artifact_tool="create_artifact" in _tool_names,
                dashboard_forced=dashboard_forced,
            ):
                logger.info(
                    "v3 stream: forcing create_artifact for pptx request "
                    "(conv=%s, iter=%d, prior_tool_count=%d)",
                    conversation_id, iteration, len(tool_calls_for_frontend),
                )
                tool_choice = {
                    "type": "function",
                    "function": {"name": "create_artifact"},
                }
                _pptx_forced = True
                _pptx_force_next_iteration = False  # consume the one-shot force
            # File-deliverable forcing (html/docx/pdf/xlsx/md): mirrors pptx
            # guard but covers non-pptx formats.  Blocked when pptx/dashboard
            # already forced.  For automation runs, detect output_format from
            # the harness metadata passed through the v3 loop.
            _file_forced = False
            if not _pptx_forced and not dashboard_forced and (
                _file_force_next_iteration or should_force_create_file(
                    user_content,
                    tool_calls_for_frontend,
                    iteration=iteration,
                    max_iterations=MAX_TOOL_ITERATIONS,
                    has_artifact_tool="create_artifact" in _tool_names,
                    dashboard_forced=dashboard_forced,
                    pptx_forced=_pptx_forced,
                    output_format=_output_format,
                )
            ):
                logger.info(
                    "v3 stream: forcing create_artifact for file deliverable "
                    "(conv=%s, iter=%d, prior_tool_count=%d, format=%s)",
                    conversation_id, iteration, len(tool_calls_for_frontend),
                    getattr(should_force_create_file, '_last_fmt', '?'),
                )
                tool_choice = {
                    "type": "function",
                    "function": {"name": "create_artifact"},
                }
                _file_forced = True
                _file_force_next_iteration = False  # consume the one-shot force

            # Goal-Contract force (flag-gated): the exit checker armed a
            # remediation force (unmet deliverable / zero-row re-query /
            # announced-but-unexecuted tool). Override tool_choice so the next
            # LLM turn MUST emit the required tool. Tool-presence was already
            # verified inside contract.unmet(); dashboard/pptx guard forcing
            # above takes precedence via the finish-line guard flag below.
            _contract_forced = False
            if (
                settings.GOAL_CONTRACT_ENABLED
                and _contract is not None
                and _contract_force_tool is not None
                and _contract_force_tool in _tool_names
            ):
                logger.info(
                    "v3 stream: goal-contract forcing %s (conv=%s, iter=%d)",
                    _contract_force_tool, conversation_id, iteration,
                )
                tool_choice = {
                    "type": "function",
                    "function": {"name": _contract_force_tool},
                }
                _contract_forced = True
                _contract_force_tool = None  # consume the one-shot force

            # Goal-Contract force-synthesis: the exit checker armed a
            # forced-synthesis (usable data already retrieved; the model just
            # needs to write the answer). Override tool_choice="none" so the
            # model must answer in prose. Fix 2's empty-answer net arms the
            # same one-shot force (via _empty_answer_force_next).
            if (
                (
                    settings.GOAL_CONTRACT_ENABLED
                    and _contract is not None
                    and _contract_force_synthesis
                )
                or _empty_answer_force_next
            ):
                logger.info(
                    "v3 stream: forcing synthesis (conv=%s, iter=%d)",
                    conversation_id, iteration,
                )
                tool_choice = "none"
                _contract_forced = True
                _contract_force_synthesis = False  # consume the one-shot force
                _empty_answer_force_next = False   # consume the one-shot force

            # Finish line: on the final iteration force tool_choice="none" so
            # the LLM must answer in text. Guard forcing (dashboard + pptx +
            # goal-contract) wins.
            tool_choice = _finish_line_tool_choice(
                iteration, MAX_TOOL_ITERATIONS - 1,
                dashboard_forced or _pptx_forced or _contract_forced, tool_choice,
            )

            # ── Gap 1: true token streaming ───────────────────────────
            # When STREAM_TOKEN_DELTAS is on, drive the streaming generator
            # (_stream_llm_with_tools) so tokens are yielded to the client
            # as they arrive (typing effect). The generator also
            # reassembles fragmented tool_calls so agent tool execution
            # continues to work. When the flag is off, fall back to the
            # legacy buffered _call_llm_with_tools path (unchanged).
            #
            # Multi-iter content capture (2026-08-20):
            # If a previous iteration produced prose (assistant_content is
            # non-empty), append it to the accumulator BEFORE resetting for
            # the next iter. Without this, multi-iter turns lose all but the
            # LAST iter's prose in the persisted message + `done` event.
            if assistant_content:
                _v3_iter_contents.append(assistant_content)
                _last_iter_prose = assistant_content  # Fix 3: save for promise-as-answer check
            assistant_content = ""
            raw_tool_calls: list = []
            final_reasoning = ""
            content_streamed = False  # tracks whether deltas were sent live
            _plan_step_seen: set = set()  # 2026-08-25: dedup plan_step_added events

            try:
                # P1.3: Pre-API deterministic tool result pruning
                prune_tool_results_only(llm_messages, model=get_model())
                sanitize_messages(llm_messages)
                # Fix D (always-on): condense oversized ask_data_agent report
                # cards before EVERY LLM call, not just the forced-synthesis
                # finish line. A reasoning model can exhaust its output budget
                # on reasoning_content mid-loop and return empty content on the
                # very next call; shrinking the report card early prevents that.
                # Idempotent: once condensed, content drops below the 6000-char
                # threshold, so re-condensing is a no-op. Full payload stays in
                # tool_calls_for_frontend (frontend report-card rendering).
                _condense_data_agent_results(llm_messages)
                # P1-5 Hermes-style proactive pruning (Step 2): between FSM
                # iterations, replace old tool results with a compact state
                # checkpoint.  This prevents accumulation of 50k+ tokens of
                # tool results across multiple data fetches in the v3 FSM
                # loop.  Deterministic, no-LLM, sub-millisecond.
                from app.services.agent_loop.fsm_pruner import prune_between_fsm_states
                llm_messages[:] = prune_between_fsm_states(
                    llm_messages, current_state="llm_call",
                )
                if STREAM_TOKEN_DELTAS:
                    # ── Streaming path ────────────────────────────────
                    # Iterate the generator: deltas → client immediately;
                    # reasoning → accumulate; terminal event → set
                    # assistant_content / raw_tool_calls.
                    # Phase 1: bounded transient retry. Only safe BEFORE the
                    # first client-visible chunk (content_streamed) — after
                    # that, a restart would duplicate emitted content, so we
                    # raise into the outer handler as before.
                    _stream_attempt = 1
                    while True:
                        try:
                            async for ev_type, ev_data in _stream_llm_with_tools(
                                llm_messages, tools, tool_choice=tool_choice,
                                model_override=user_model,
                                endpoint=effective_llm.endpoint,
                                temperature=llm_overrides.get("temperature"),
                                max_tokens=llm_overrides.get("max_tokens"),
                            ):
                                if ev_type == "delta":
                                    assistant_content += ev_data
                                    content_streamed = True
                                    # 2026-08-25: live-streaming spec — parse the
                                    # streaming text for plan-step markers and emit
                                    # `plan_step_added` for each newly-detected step.
                                    # 2026-08-27: the deterministic turn plan
                                    # (TURN_PLAN_ENABLED) is now the ONLY source of
                                    # plan_step_added — it is emitted BEFORE the loop
                                    # with clean titles and ticked off by tool
                                    # evidence. This legacy streaming parser collided
                                    # with it: it re-parsed the streamed markdown on
                                    # every delta, captured mid-stream partial lines
                                    # ("- **") as step titles, and its step_indexes
                                    # overwrote the turn plan's titles in the UI
                                    # checklist. Kept only for TURN_PLAN_ENABLED=False
                                    # (legacy) mode.
                                    if not settings.TURN_PLAN_ENABLED:
                                        try:
                                            from app.services.agent_loop.streaming_helpers import parse_plan_steps_from_text
                                            for _step in parse_plan_steps_from_text(assistant_content):
                                                if _step["step_index"] not in _plan_step_seen:
                                                    _plan_step_seen.add(_step["step_index"])
                                                    yield f'data: {json.dumps({"type": "plan_step_added", "step_index": _step["step_index"], "title": _step["title"]})}\n\n'
                                        except Exception as _plan_err:
                                            logger.debug("plan step parse failed (non-fatal): %s", _plan_err)
                                    if _nudge_replacement_pending:
                                        # D1/D2: post-nudge re-synthesis replaces
                                        # the whole bubble instead of appending, so
                                        # leaked nudge-reply prose never shows.
                                        # 2026-08-28: one-shot latch — emit the
                                        # replacement ONCE (full accumulated text),
                                        # then clear the flag so the rest of the
                                        # stream appends as normal deltas. Previously
                                        # the flag was never reset, so EVERY token
                                        # became a full-bubble content_replace (629
                                        # in one CAD turn = the visible
                                        # buffering/collapse loop).
                                        if not _suppress_chat_deltas:
                                            yield f'data: {json.dumps({"type": "content_replace", "content": assistant_content})}\n\n'
                                        _nudge_replacement_pending = False
                                    else:
                                        if not _suppress_chat_deltas:
                                            yield f'data: {json.dumps({"type": "delta", "content": ev_data})}\n\n'
                                elif ev_type == "reasoning":
                                    final_reasoning += ev_data
                                    # 2026-08-25: live-streaming spec — emit per-token reasoning to client
                                    # so the UI can show character-by-character "thinking" (Kimi/Claude-style).
                                    yield f'data: {json.dumps({"type": "reasoning_delta", "content": ev_data})}\n\n'
                                elif ev_type == "tool_calls":
                                    raw_tool_calls = ev_data or []
                                    # qwen3 content-based tool_call extraction:
                                    # the thinking preamble was already streamed
                                    # as deltas. Replace it with clean content
                                    # so the chat bubble shows the tool result
                                    # instead of raw thinking.
                                    if content_streamed and assistant_content:
                                        cleaned = _strip_tool_call_markup(assistant_content)
                                        # Also strip SQL/plan narration
                                        # ("SELECT …" fences, bare SQL
                                        # paragraphs): this iteration is
                                        # calling tools, so the prose is
                                        # plan narration, not the answer
                                        # ("SQL leak", 2026-08-21).
                                        cleaned = _strip_sql_narration(cleaned)
                                        if cleaned != assistant_content:
                                            assistant_content = cleaned
                                            if not _suppress_chat_deltas:
                                                yield f'data: {json.dumps({"type": "content_replace", "content": assistant_content})}\n\n'
                                elif ev_type == "done":
                                    # Generator's authoritative full text — use
                                    # it if we didn't accumulate (defensive; in
                                    # practice assistant_content already matches).
                                    if ev_data and not assistant_content:
                                        assistant_content = ev_data
                                elif ev_type == "error":
                                    raise RuntimeError(ev_data)
                            break  # stream completed cleanly
                        except Exception as stream_err:
                            ce_stream = classify_api_error(stream_err)
                            if (
                                content_streamed
                                or not is_transient(ce_stream)
                                or _stream_attempt >= max_attempts_for(ce_stream)
                            ):
                                raise
                            delay = next_backoff(
                                _stream_attempt - 1, retry_after_seconds(stream_err)
                            )
                            _stream_attempt += 1
                            logger.warning(
                                "stream: transient %s in conversation %s; "
                                "retry %d/%d in %.1fs",
                                ce_stream.reason.value, conversation_id,
                                _stream_attempt, max_attempts_for(ce_stream), delay,
                            )
                            yield f'data: {json.dumps({"type": "llm_retry", "attempt": _stream_attempt, "max_attempts": max_attempts_for(ce_stream), "delay_ms": int(delay * 1000), "reason": ce_stream.reason.value})}\n\n'
                            await asyncio.sleep(delay)
                else:
                    # ── Legacy buffered path ──────────────────────────
                    llm_response = await _call_llm_with_tools(
                        llm_messages, tools, tool_choice=tool_choice,
                        model_override=user_model,
                        endpoint=effective_llm.endpoint,
                        temperature=llm_overrides.get("temperature"),
                        max_tokens=llm_overrides.get("max_tokens"),
                    )
                    assistant_content = llm_response.get("content", "")
                    raw_tool_calls = llm_response.get("tool_calls", [])
                    final_reasoning = llm_response.get("reasoning", "") or ""
            except Exception as e:
                # P1.1: Structured error classification
                ce = classify_api_error(e)
                logger.warning(
                    "LLM call error in conversation %s (stream): reason=%s retryable=%s should_compress=%s err=%r",
                    conversation_id, ce.reason.value, ce.retryable, ce.should_compress, e,
                )
                metrics.record_error(ce.reason.value)
                # Phase 1: honor classifier hints the stream path used to
                # drop. Only safe before any client-visible content was
                # emitted (content_streamed False).
                if not content_streamed and ce.should_fallback:
                    # Switch model for a final buffered attempt. Seamless:
                    # nothing has streamed yet, so emitting the buffered
                    # content as one delta is indistinguishable to the client.
                    try:
                        async def _call_with_fallback_model(_model_name):
                            return await _call_llm_with_tools(
                                llm_messages, tools, tool_choice=tool_choice,
                                model_override=_model_name,
                                endpoint=effective_llm.endpoint,
                                temperature=llm_overrides.get("temperature"),
                                max_tokens=llm_overrides.get("max_tokens"),
                            )
                        _fb_response, _fb_model = await with_fallback(
                            get_model(), _call_with_fallback_model, ce,
                            user_fallback=llm_overrides.get("fallback_model"),
                        )
                    except Exception as _fb_err:
                        _fb_response, _fb_model = None, None
                        logger.warning("stream: fallback attempts failed: %s", _fb_err)
                    if _fb_response:
                        assistant_content = _fb_response.get("content", "")
                        raw_tool_calls = _fb_response.get("tool_calls", [])
                        final_reasoning = _fb_response.get("reasoning", "") or ""
                        logger.info("stream: fallback to model %s succeeded", _fb_model)
                        if assistant_content:
                            content_streamed = True
                            if _nudge_replacement_pending:
                                # D1/D2: full-content emit must replace the
                                # bubble when a re-synthesis is in flight.
                                # 2026-08-28: one-shot (see delta branch).
                                if not _suppress_chat_deltas:
                                    yield f'data: {json.dumps({"type": "content_replace", "content": assistant_content})}\n\n'
                                _nudge_replacement_pending = False
                            else:
                                if not _suppress_chat_deltas:
                                    yield f'data: {json.dumps({"type": "delta", "content": assistant_content})}\n\n'
                        # Fall through to the post-try flow (reasoning_done,
                        # tool-call handling) instead of failing the turn.
                    else:
                        for _err_event in _persist_stream_error(
                            db, conv, conversation_id, messages, e, ce,
                            tool_calls_for_frontend, artifact_ids,
                        ):
                            yield _err_event
                        _discard_steer(conversation_id)
                        return
                elif (
                    not content_streamed
                    and ce.should_compress
                    and not _stream_compaction_attempted[0]
                ):
                    # Context overflow: one reactive compaction pass, then
                    # re-enter the iteration with compacted messages.
                    _stream_compaction_attempted[0] = True
                    try:
                        from app.services.compaction import (
                            AutoCompactState,
                            auto_compact_if_needed,
                        )
                        # NOTE: must NOT rebind `llm_messages` here — it is a
                        # closure variable from add_message_stream, and any
                        # assignment would make it event_stream-local, turning
                        # every earlier read into UnboundLocalError (kills the
                        # SSE stream). Mutate in place instead.
                        _compacted, was_compacted = await auto_compact_if_needed(
                            llm_messages,
                            # FIX 2026-08-24 (v2): the compactor's LLM call
                            # needs MORE context than the user's model to
                            # actually summarize a 60k+ token conversation
                            # when the user is on a small-context model
                            # (qwen3.6-27b = 65k).  Use the global default
                            # (deepseek-v4-flash, 128k) so the compactor's
                            # LLM call doesn't itself overflow.
                            model=get_model(),
                            state=_compaction_states.get(
                                conversation_id, AutoCompactState()
                            ),
                            force=True,
                            trigger="reactive",
                        )
                    except Exception as compact_err:
                        logger.error("stream: reactive compaction failed: %s", compact_err)
                        was_compacted = False
                    if was_compacted:
                        if not _compacted or _compacted[0].get("role") != "system":
                            _compacted.insert(0, {"role": "system", "content": system_prompt})
                        llm_messages[:] = _compacted
                        continue
                    for _err_event in _persist_stream_error(
                        db, conv, conversation_id, messages, e, ce,
                        tool_calls_for_frontend, artifact_ids,
                    ):
                        yield _err_event
                    _discard_steer(conversation_id)
                    return
                else:
                    for _err_event in _persist_stream_error(
                        db, conv, conversation_id, messages, e, ce,
                        tool_calls_for_frontend, artifact_ids,
                    ):
                        yield _err_event
                    _discard_steer(conversation_id)
                    return

            # P0: surface reasoning captured from the LLM message.
            # The non-streaming _call_llm_with_tools puts DeepSeek-R1's
            # reasoning_content at message.reasoning_content; we forward it
            # as a single 'reasoning_done' SSE event per iteration and
            # accumulate it into reasoning_acc for final persistence on
            # assistant_msg (the assistant_msg dict is built later, after
            # the loop ends — see line ~4000).
            # NOTE: 'fsm_state' event type is reserved for the future
            # SynexiaFSM-in-SSE follow-up. It will share this envelope; the
            # FSM design is separate.
            if final_reasoning:
                reasoning_acc.append(final_reasoning)
                yield f'data: {json.dumps({"type": "reasoning_done", "reasoning": final_reasoning, "step_count": len(tool_calls_for_frontend)})}\n\n'

            # ── Clarification hard-stop (:::options) — same as the
            # add_message / resume loops: a message with an options block
            # asks the user a question → turn done, no tools, no
            # goal-contract / verification gates (they would misread the
            # question as an unmet deliverable and force a retry loop).
            _opt_names = [tc.get("function", {}).get("name", "") for tc in raw_tool_calls]
            if _options_clarification(assistant_content, _opt_names):
                if raw_tool_calls:
                    logger.info(
                        "Suppressing %d tool call(s) after :::options clarification "
                        "block (stream, conv=%s, iter=%d): %s",
                        len(raw_tool_calls), conversation_id, iteration, _opt_names,
                    )
                    raw_tool_calls = []
                break

            if not raw_tool_calls:
                # ── Goal-Contract exit checker (flag-gated) ──────────
                # FIRST in the exit branch: if the turn contract has unmet
                # criteria (deliverable unproduced / zero-rows-with-text-
                # filters / an announced-but-unexecuted tool), inject the
                # remediation nudge and arm the tool_choice force for the
                # next iteration, then continue the loop. When the contract
                # is satisfied (or the force budget is exhausted) the legacy
                # guardrail → verify-on-stop → pptx-guard → self-eval chain
                # below runs unchanged (flag-off = current behavior).
                if settings.GOAL_CONTRACT_ENABLED and _contract is not None:
                    # Re-evaluate the pending-action marker from the model's
                    # latest prose. The announce-but-don't-execute pattern
                    # ("Let me check the live tables...") only surfaces in
                    # assistant_content at exit — the turn-start value comes
                    # from user_content alone. Only runs in the no-tool-calls
                    # exit branch (cheap; the criterion matters at exit).
                    _contract.refresh_pending_action(assistant_content)
                    # Fix 4 (extended): clean exit — whenever the model's last
                    # sentence announces a pending action ("Let me verify…"),
                    # strip that trailing sentence. Previously this only fired
                    # when forces_used >= max_forces, but the promise-as-answer
                    # bug also happens when the model exits normally with
                    # promise text after producing a valid artifact — the user
                    # sees the artifact AND the misleading "Let me verify…"
                    # appended to it. Now we ALWAYS strip + append the note.
                    _pending_phrase = pending_action_phrase(assistant_content)
                    _stripped_promise = False
                    if _pending_phrase:
                        _stripped_promise = True
                        assistant_content = _strip_trailing_pending(
                            assistant_content, _pending_phrase,
                        )
                        # Only append the limitation note if the stripped
                        # content is non-empty (the rest of the answer is
                        # real content that stands on its own). When the
                        # pending sentence IS the whole reply, the empty-
                        # answer net below will handle it.
                        if assistant_content.strip():
                            assistant_content = (
                                assistant_content + "\n\nNote: this turn ended before completing the "
                                "additional verification described above. The current answer is based "
                                "on the data already returned."
                            ).strip()
                    _gc_unmet = _contract.unmet(_tool_names_from_schemas(tools))
                    if _gc_unmet:
                        _gc_crit = _gc_unmet[0]
                        _contract.record_force()
                        if _gc_crit.force_synthesis:
                            _contract_force_synthesis = True
                        else:
                            _contract_force_tool = _gc_crit.force_tool
                        llm_messages.append({"role": "assistant", "content": assistant_content})
                        llm_messages.append({"role": "user", "content": _gc_crit.message})
                        logger.info(
                            "v3 stream: goal-contract unmet (%s) → forcing %s "
                            "(conv=%s, iter=%d, forces=%d/%d)",
                            _gc_crit.code,
                            "synthesis" if _gc_crit.force_synthesis else _gc_crit.force_tool,
                            conversation_id,
                            iteration, _contract.forces_used, _contract.max_forces,
                        )
                        # Pop the just-captured iter content: the next iter
                        # will re-emit (corrected) prose and we don't want
                        # to accumulate both copies in `_v3_iter_contents`.
                        # BUGFIX 2026-08-23: save popped good content for recovery.
                        if _v3_iter_contents:
                            _popped = _v3_iter_contents.pop()
                            if len(_popped or "") > len(_v3_recovered_best[0] or ""):
                                _v3_recovered_best[0] = _popped
                        # D1/D2: nudge-reply prose must never be visible. Clear
                        # it (the top-of-loop append skips it) and mark the
                        # next iteration's stream as a bubble replacement.
                        # 2026-08-31: emit content_preserve (NOT a bare
                        # content_replace) so the frontend keeps the old text
                        # visible with a "Refining answer…" indicator while the
                        # re-synthesis runs — the bare replace was the visible
                        # collapse-then-regenerate UX. The next iteration's
                        # first delta (via _nudge_replacement_pending) swaps in
                        # the real replacement text.
                        if content_streamed and assistant_content and not _suppress_chat_deltas:
                            yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "goal_contract"})}\n\n'
                        assistant_content = ""
                        _nudge_replacement_pending = True
                        continue
                # ── Promise-stripped force synthesis (Fix 5b) ───────────
                # When the model's FINAL reply was ONLY a pending-action
                # promise ("Let me get a clean view…"), we stripped it above.
                # The user has already received the promise text via SSE, but
                # the persisted message is now empty. The empty-answer net
                # below would refuse to fire because content_streamed=True —
                # but the content that was streamed was a PROMISE, not an
                # answer. Force one re-synthesis pass so the persisted
                # message becomes a real answer. The frontend's content_replace
                # event will swap the promise bubble for the real answer.
                _has_usable_data = (
                    (_contract is not None and _contract._usable_results > 0)
                    if settings.GOAL_CONTRACT_ENABLED
                    else any(
                        tc.get("status") == "completed"
                        for tc in tool_calls_for_frontend
                    )
                )
                if (
                    _stripped_promise
                    and not assistant_content.strip()
                    and _has_usable_data
                    and _empty_answer_forces < 1
                    and settings.GOAL_CONTRACT_ENABLED
                ):
                    _empty_answer_forces += 1
                    _empty_answer_force_next = True
                    _contract.record_force()
                    _contract_force_synthesis = True
                    _nudge_msg = (
                        "You announced a follow-up action but did not execute it. "
                        "The data you need is already in your context. "
                        "Write the final answer now using the retrieved data. "
                        "Do not announce future actions."
                    )
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _nudge_msg})
                    # BUGFIX 2026-08-23: save popped good content for recovery.
                    if _v3_iter_contents:
                        _popped = _v3_iter_contents.pop()
                        if len(_popped or "") > len(_v3_recovered_best[0] or ""):
                            _v3_recovered_best[0] = _popped
                    # Tell the frontend to swap the streamed promise text
                    # for the upcoming synthesis bubble.
                    # 2026-08-25: emit content_preserve first so the
                    # frontend can keep old text visible during the swap,
                    # avoiding the "collapse" UX.
                    # 2026-08-28: do NOT also emit content_replace "" here —
                    # that wiped the bubble to empty (visible collapse) before
                    # the re-synthesis streamed. content_preserve keeps the old
                    # text visible; the re-synthesis's first delta arrives as a
                    # content_replace (flag set below) with the REAL text.
                    yield f'data: {json.dumps({"type": "content_preserve", "content": "", "reason": "promise_strip"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    logger.info(
                        "v3 stream: stripped promise at exit → forcing synthesis "
                        "(conv=%s, iter=%d, forces=%d/%d)",
                        conversation_id, iteration,
                        _empty_answer_forces, 1,
                    )
                    continue
                # ── Apology-guard (Fix 5) ────────────────────────────────
                # Model "answered" with the generic apology ("I gathered some
                # information but had trouble putting it all together") even
                # though usable data was retrieved. This is NOT an empty
                # answer — it's an unhelpful non-answer — so the empty-answer
                # net below would miss it. Force one re-synthesis pass via the
                # same _empty_answer_force machinery (capped at 1/turn).
                if (
                    _APOLOGY_PATTERN_RE.search(assistant_content or "")
                    and _has_usable_data
                    and _empty_answer_forces < 1
                ):
                    _empty_answer_forces += 1
                    _empty_answer_force_next = True
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({
                        "role": "user",
                        "content": (
                            "Your previous reply was an apology, not an answer — "
                            "but you already have the data needed to answer. "
                            "Write the final answer now in prose, using the tool "
                            "results above. Do not apologize and do not call tools."
                        ),
                    })
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        # 2026-08-31: content_preserve (see guardrail site).
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "apology_force"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    logger.warning(
                        "v3 stream: apology detected with usable data → forcing "
                        "synthesis retry (conv=%s, iter=%d, forces=%d/%d)",
                        conversation_id, iteration,
                        _empty_answer_forces, 1,
                    )
                    continue
                # ── Bounce-back guard (Fix 7) ──────────────────────────────
                # The agent retrieved data but instead of answering, it dumped
                # a table + "You can ask me for a summary". This is NOT an
                # answer — force one re-synthesis pass (same cap as apology).
                if (
                    _BOUNCE_BACK_PATTERN_RE.search(assistant_content or "")
                    and _has_usable_data
                    and _empty_answer_forces < 1
                ):
                    _empty_answer_forces += 1
                    _empty_answer_force_next = True
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({
                        "role": "user",
                        "content": (
                            "Your previous reply just dumped raw data instead of "
                            "answering the user's question. The user asked a real "
                            "question and expects a real answer. Write a comprehensive "
                            "answer now using the data you already have. Do NOT invite "
                            "the user to ask again and do NOT call tools."
                        ),
                    })
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        # 2026-08-31: content_preserve (see guardrail site).
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "bounce_back_force"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    logger.warning(
                        "v3 stream: bounce-back detected with usable data → forcing "
                        "synthesis retry (conv=%s, iter=%d, forces=%d/%d)",
                        conversation_id, iteration,
                        _empty_answer_forces, 1,
                    )
                    continue
                # ── Empty-answer safety net (Fix 2) ─────────────────────
                # Model stopped with NO visible prose (reasoning-budget
                # exhaustion) but usable data was retrieved → force one
                # synthesis retry (tool_choice="none" next iteration, which
                # also runs Fix 1 condensation so the retry has a real
                # chance of producing a prose answer).
                _has_usable_data = (
                    (_contract is not None and _contract._usable_results > 0)
                    if settings.GOAL_CONTRACT_ENABLED
                    else any(
                        tc.get("status") == "completed"
                        for tc in tool_calls_for_frontend
                    )
                )
                if _empty_answer_needs_force(
                    assistant_content, content_streamed, _v3_iter_contents,
                    _empty_answer_forces, _has_usable_data,
                ):
                    _empty_answer_forces += 1
                    _empty_answer_force_next = True
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({
                        "role": "user",
                        "content": (
                            "Your previous response was empty. You already have "
                            "the data needed to answer. Write the final answer "
                            "now in prose, using the tool results above. Do not "
                            "call any tools."
                        ),
                    })
                    # Replace any previously streamed text (promise narration
                    # from earlier iterations) with the upcoming synthesis.
                    # 2026-08-31: content_preserve instead of a bare
                    # content_replace — keep old text + "Refining answer…"
                    # indicator visible during the forced retry.
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "empty_answer_force"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    logger.warning(
                        "v3 stream: empty final answer with usable data; forcing "
                        "one synthesis retry (conv=%s, iter=%d)",
                        conversation_id, iteration,
                    )
                    continue
                guardrail_result = _check_hallucination_guardrail(
                    user_content, data_ctx_extras, tool_calls_for_frontend,
                    iteration, guardrail_retries,
                )
                if guardrail_result.action == "nudge":
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": guardrail_result.message})
                    guardrail_retries += 1
                    # D1/D2: guardrail nudge-reply must not leak into the
                    # final answer (same clear + preserve pattern).
                    # 2026-08-31: content_preserve — the hallucination guardrail
                    # fires when the LLM answered a data question WITHOUT
                    # calling a tool; the streamed answer was fabricated, so the
                    # next retry replaces it. Emitting a bare content_replace
                    # here made the bubble visibly collapse-then-regenerate
                    # (the exact UX reported). preserve keeps the old text +
                    # "Refining answer…" indicator during the retry; the next
                    # iteration's first delta swaps in the real answer.
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "hallucination_guardrail"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    continue
                elif guardrail_result.action == "fallback":
                    assistant_content = guardrail_result.message
                    break
                # No guardrail trigger — stream the final response
                # P2.1: Verification-on-stop
                # Fix 1c: the verify-nudge shares the goal-contract force budget.
                # When the contract is active, a nudge is only issued while
                # forces_used < min(VERIFY_NUDGE_MAX, GOAL_CONTRACT_MAX_FORCES);
                # issuing one records a force so a nudge can never extend the
                # turn beyond the global force budget. Contract inactive (flag
                # off) → standalone cap via settings.VERIFY_NUDGE_MAX.
                # ── CAD verify-on-stop (2026-08-27) ────────────────────────
                # A Fusion build turn MUST end with fusion360_verify_build before
                # claiming success. The CAD prompt mandates it but qwen3.6-27b
                # frequently skips it and declares "build finished" after a
                # partial build (observed: M5 bolt+nut left only a hex head on
                # canvas). If modeling tools ran this turn and verify_build was
                # never called, nudge the model to call it now (cap 1/turn —
                # the nudge forces a tool-call next iteration, and the result
                # feeds _cad_build_fallback deterministically).
                if (
                    _is_fusion_agent
                    and _fusion_build_progress_this_turn(tool_calls_for_frontend)
                    and not _tool_was_called(tool_calls_for_frontend, "fusion360_verify_build")
                    and _cad_verify_nudges < 1
                ):
                    _cad_verify_nudges += 1
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({
                        "role": "user",
                        "content": (
                            "You ran Fusion 360 modeling tools this turn but never "
                            "called fusion360_verify_build. Call it NOW with "
                            "expected_body_count=<the number of parts in your plan> "
                            "(and expected_params if you created parameters). Read the "
                            "PASS/FAIL result — if it FAILs, fix the discrepancy before "
                            "finishing. Do not claim the build is complete until "
                            "verify_build returns ok=true."
                        ),
                    })
                    # D1/D2: nudge reply must not leak into the final answer.
                    # 2026-08-31: content_preserve (see guardrail site).
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "cad_verify"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    logger.info(
                        "v3 stream: CAD verify-on-stop nudge injected (conv=%s, iter=%d)",
                        conversation_id, iteration,
                    )
                    continue
                _verify_nudge = build_verify_on_stop_nudge(llm_messages, attempts=_verify_attempts)
                if _verify_nudge and _contract is not None and _contract.forces_used >= min(
                    _effective_verify_nudge_max(effective_llm), settings.GOAL_CONTRACT_MAX_FORCES,
                ):
                    _verify_nudge = None
                if _verify_nudge:
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _verify_nudge})
                    _verify_attempts += 1
                    if _contract is not None:
                        _contract.record_force()
                    # D1/D2: verify nudge-reply must not leak into the final
                    # answer (same clear + preserve pattern).
                    # 2026-08-31: content_preserve (see guardrail site).
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "verify_nudge"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    continue
                # P2.1.5: PPTX turn-guard (v3 loop) — if the user asked for a
                # PPT deck and the model ended its turn with text only (no tool
                # call), nudge it to call create_artifact(type="pptx") now
                # (cap 1/turn). Runs BEFORE the self-eval gate.
                _pptx_guard = pptx_turn_guard(
                    user_content,
                    tool_calls_for_frontend,
                    budget_remaining=MAX_TOOL_ITERATIONS - iteration,
                    attempts=_pptx_nudge_attempts,
                )
                if _pptx_guard.action == "nudge":
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _pptx_guard.message})
                    _pptx_nudge_attempts += 1
                    # Fix 1b: the LAST allowed nudge arms a one-shot force so
                    # the next iteration forces create_artifact (the model can
                    # no longer deflect in prose).
                    if _pptx_guard.force_next:
                        _pptx_force_next_iteration = True
                    logger.info(
                        "v3 stream: pptx turn-guard nudge injected (conv=%s, iter=%d, force_next=%s)",
                        conversation_id, iteration, _pptx_guard.force_next,
                    )
                    # Pop captured iter content: nudge injects assistant_content
                    # to llm_messages so the LLM sees it as context; the next
                    # iter's re-emit replaces it (avoid duplicating the entire
                    # response in `_v3_iter_contents`).
                    # BUGFIX 2026-08-23: save popped good content for recovery.
                    if _v3_iter_contents:
                        _popped = _v3_iter_contents.pop()
                        if len(_popped or "") > len(_v3_recovered_best[0] or ""):
                            _v3_recovered_best[0] = _popped
                    # D1/D2: pptx nudge-reply must not leak into the final
                    # answer (same clear + preserve pattern).
                    # 2026-08-31: content_preserve (see guardrail site).
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "pptx_guard"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    continue
                if _pptx_guard.action == "disclose":
                    logger.warning(
                        "v3 stream: pptx deliverable not generated; disclosure "
                        "appended (conv=%s, iter=%d)", conversation_id, iteration,
                    )
                    assistant_content = (assistant_content or "") + " " + _pptx_guard.message
                # P2.1.6: File-deliverable turn-guard (v3 loop) — mirrors pptx
                # guard for html/docx/pdf/xlsx/md.  When the user asked for a
                # file deliverable (or automation output_format), nudge the
                # model to call create_artifact.  Blocked when pptx guard
                # already nudged/forced this iteration.
                if _pptx_guard.action == "none":
                    _file_guard = file_turn_guard(
                        user_content,
                        tool_calls_for_frontend,
                        budget_remaining=MAX_TOOL_ITERATIONS - iteration,
                        attempts=_file_nudge_attempts,
                        output_format=_output_format,
                    )
                    if _file_guard.action == "nudge":
                                        llm_messages.append({"role": "assistant", "content": assistant_content})
                                        llm_messages.append({"role": "user", "content": _file_guard.message})
                                        _file_nudge_attempts += 1
                                        if _file_guard.force_next:
                                            _file_force_next_iteration = True
                                        logger.info(
                                            "v3 stream: file turn-guard nudge injected "
                                            "(conv=%s, iter=%d, format=%s, force_next=%s)",
                                            conversation_id, iteration,
                                            _file_guard.detected_format, _file_guard.force_next,
                                        )
                                        # Pop captured iter content: see pptx nudge site above
                                        # for rationale. Prevents the report from being appended
                                        # twice to `_v3_iter_contents` after the agent re-emits.
                                        # BUGFIX 2026-08-23: save popped good content for recovery.
                                        if _v3_iter_contents:
                                            _popped = _v3_iter_contents.pop()
                                            if len(_popped or "") > len(_v3_recovered_best[0] or ""):
                                                _v3_recovered_best[0] = _popped
                                        # D1/D2: file-guard nudge-reply must not
                                        # leak into the final answer.
                                        # 2026-08-31: content_preserve (see
                                        # guardrail site).
                                        if content_streamed and assistant_content and not _suppress_chat_deltas:
                                            yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "file_guard"})}\n\n'
                                        assistant_content = ""
                                        _nudge_replacement_pending = True
                                        continue
                    if _file_guard.action == "disclose":
                        logger.warning(
                            "v3 stream: file deliverable not generated; disclosure "
                            "appended (conv=%s, iter=%d, format=%s)",
                            conversation_id, iteration, _file_guard.detected_format,
                        )
                        assistant_content = (assistant_content or "") + " " + _file_guard.message
                # P2.2: Universal Self-Evaluation & Re-Planning gate (v3 loop)
                # 2026-08-25: data-sufficient fast-path. When the agent has
                # usable data AND already produced substantive prose (>= N
                # chars) AND the gate has already nudged once, skip further
                # nudges and fall through to finalize. The 2nd/3rd nudges
                # almost never improve quality and only cause the
                # "collapse" UX where streamed text gets replaced. The
                # `pending_action` post-loop strip (further down) will
                # still clean up any trailing promise phrases.
                _data_sufficient_skip = False
                if (
                    _gate_attempts >= 1
                    and assistant_content
                    and len(assistant_content.strip())
                    >= int(
                        getattr(
                            settings,
                            "SELF_EVAL_DATA_SUFFICIENT_MIN_PROSE",
                            200,
                        )
                    )
                ):
                    _has_usable_for_skip = (
                        (_contract is not None and _contract._usable_results > 0)
                        if settings.GOAL_CONTRACT_ENABLED
                        else any(
                            tc.get("status") == "completed"
                            for tc in tool_calls_for_frontend
                        )
                    )
                    if _has_usable_for_skip:
                        _data_sufficient_skip = True
                        logger.info(
                            "v3 stream: data-sufficient fast-path — skipping "
                            "self-eval nudge (conv=%s, iter=%d, prose=%d "
                            "chars, gate_attempts=%d)",
                            conversation_id,
                            iteration,
                            len(assistant_content.strip()),
                            _gate_attempts,
                        )
                if _data_sufficient_skip:
                    # Don't call the gate at all; fall through to finalize
                    # block below. The post-loop promise-strip will clean
                    # up any trailing "Let me..." sentence.
                    _gate_result = None
                else:
                    _gate_result = await _check_answer_verification_gate(
                        user_content,
                        tool_calls_for_frontend,
                        assistant_content,
                        attempts=_gate_attempts,
                        budget_remaining=MAX_TOOL_ITERATIONS - iteration,
                        catalog_meta=(data_ctx_extras or {}).get("catalog_meta"),
                    )
                if _gate_result is not None and _gate_result.action == "nudge":
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _gate_result.message})
                    _gate_attempts += 1
                    # Pop captured iter content: the gate nudged because it
                    # thinks a dimension is missing; the next iter's prose
                    # will replace this entry instead of duplicating it
                    # (the user's repro: "Daily Sales Data Sync" produced
                    # the report twice because markdown/outcome/running
                    # triggered a re-iteration).
                    # BUGFIX 2026-08-23: save popped good content for recovery.
                    if _v3_iter_contents:
                        _popped = _v3_iter_contents.pop()
                        if len(_popped or "") > len(_v3_recovered_best[0] or ""):
                            _v3_recovered_best[0] = _popped
                    # D1/D2: gate nudge-reply prose must never be visible.
                    # Clear it so the top-of-loop append skips it; the next
                    # iteration's stream replaces the bubble instead of
                    # appending (leakage fix).
                    # 2026-08-25: emit a content_preserve event BEFORE the
                    # content_replace so the frontend can keep the old
                    # text visible (with a "Refining answer..." indicator)
                    # while the next iteration streams its replacement.
                    # This eliminates the "collapse" visual where text
                    # disappears and reappears. The content_replace
                    # immediately follows to swap the persisted message
                    # content; the frontend preserves the visual bubble.
                    # 2026-08-31: DROP the immediate content_replace — it
                    # cleared the refining flag instantly (the indicator
                    # never showed). preserve-only matches promise_strip:
                    # the next iteration's first delta (via
                    # _nudge_replacement_pending) performs the actual swap.
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "self_eval_nudge"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    continue
                if _gate_result is not None and _gate_result.action == "disclose":
                    assistant_content = (assistant_content or "") + _gate_result.message
                # ── Fix 6: dashboard-narration nudge guard ─────────────
                # The model exited with ONLY narration ("I'll build you an ERP
                # dashboard...") and no tool call. None of the six guards above
                # fired because narration IS content (empty-answer net skips),
                # no data was retrieved (promise-strip skips), and no tools ran
                # (self-eval returns "none"). When the flag is on and the build
                # tool hasn't been called yet, inject a hard nudge naming the
                # EXACT next workflow step and continue the loop (capped per
                # turn). Confirmation questions after describe_schema are
                # allowed by the prompt HARD RULE and are skipped here.
                if (
                    getattr(settings, "DASHBOARD_NARRATION_NUDGE_ENABLED", False)
                    and dashboard_narration_needs_nudge(
                        user_content,
                        _v3_executed_tool_names,
                        _dashboard_narration_nudges,
                        settings.MAX_DASHBOARD_NARRATION_NUDGES,
                        narration=assistant_content,
                    )
                ):
                    _nudge_message = build_dashboard_narration_nudge_message(
                        _v3_executed_tool_names, _dash_build_tool,
                    )
                    _dashboard_narration_nudges += 1
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _nudge_message})
                    logger.info(
                        "v3 stream: dashboard narration nudge injected "
                        "(conv=%s, iter=%d, nudge=%d/%d)",
                        conversation_id, iteration,
                        _dashboard_narration_nudges,
                        settings.MAX_DASHBOARD_NARRATION_NUDGES,
                    )
                    # Pop captured iter content: see file-guard nudge site for
                    # rationale — the re-emitted iteration must replace the
                    # narration bubble instead of appending a duplicate.
                    # BUGFIX 2026-08-23: save popped good content for recovery.
                    if _v3_iter_contents:
                        _popped = _v3_iter_contents.pop()
                        if len(_popped or "") > len(_v3_recovered_best[0] or ""):
                            _v3_recovered_best[0] = _popped
                    # D1/D2: the nudge reply must never leak into the final
                    # answer (same preserve SSE pattern as the other nudge
                    # sites). 2026-08-31: content_preserve (see guardrail site).
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "dashboard_narration"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    continue
                # ── P1-4c: hard delegation enforcement (2026-08-29) ──────
                # The model answered a parallelizable ask with a weak final
                # answer and never called delegate_task. Mirror the narration
                # nudge: inject the hard instruction, drop the weak narration,
                # and continue the loop (capped once per turn).
                if (
                    _delegation_nudged
                    and not _delegation_enforced
                    and "delegate_task" not in _v3_executed_tool_names
                ):
                    _delegation_enforced = True
                    _enforce_msg = (
                        "SYSTEM GUARDRAIL: your answer did not cover all the "
                        "requested items and you did not call delegate_task. "
                        "The user's ask has INDEPENDENT parts (top-N lists / "
                        "comparisons). Call `delegate_task(tasks=[...])` NOW — "
                        "ONE task string per item — then synthesize their "
                        "results. Do not reply with prose until you have made "
                        "that call."
                    )
                    llm_messages.append({"role": "assistant", "content": assistant_content or ""})
                    llm_messages.append({"role": "user", "content": _enforce_msg})
                    if _v3_iter_contents:
                        _popped = _v3_iter_contents.pop()
                        if len(_popped or "") > len(_v3_recovered_best[0] or ""):
                            _v3_recovered_best[0] = _popped
                    if content_streamed and assistant_content and not _suppress_chat_deltas:
                        # 2026-08-31: content_preserve (NOT the old
                        # content_replace with "") — the empty replace WIPED
                        # the visible bubble (a literal collapse); preserve
                        # keeps the weak narration visible + "Refining
                        # answer…" while the delegation-enforced next
                        # iteration streams.
                        yield f'data: {json.dumps({"type": "content_preserve", "content": assistant_content, "reason": "delegation_enforce"})}\n\n'
                    assistant_content = ""
                    _nudge_replacement_pending = True
                    logger.warning(
                        "v3 stream: hard delegation enforcement injected "
                        "(conv=%s, iter=%d)",
                        conversation_id, iteration,
                    )
                    continue
                break

            # Parse and execute tool calls (parallel for multiple)
            parsed_calls = []
            for tc in raw_tool_calls:
                func = tc.get("function", {})
                tool_name = func.get("name", "")
                args_str = func.get("arguments", "{}")
                tool_call_id = tc.get("id", str(uuid.uuid4()))
                try:
                    args = json.loads(args_str) if isinstance(args_str, str) else args_str
                except json.JSONDecodeError:
                    args = {}
                parsed_calls.append({
                    "tool_name": tool_name, "args": args,
                    "args_str": args_str, "tool_call_id": tool_call_id,
                })

            # ── Batch-replay guard (2026-08-27) ─────────────────────────
            # The single-tool loop guard keys on (tool_name, args) per call,
            # so a large parallel batch re-emitted verbatim N times (each tool
            # called once per batch) never accumulates a per-tool count — the
            # classic "identical 25-tool batch replayed 24×" runaway. Fingerprint
            # the WHOLE batch (sorted name+args) and break when the same batch
            # repeats >= 3×, nudging the model to stop.
            # 2026-08-28: only guard batches with >= 2 calls. A legit CAD
            # re-sync can call fusion360_info (single call, identical args)
            # 3+ times in a row — tripping on single-call batches killed the
            # turn mid-re-sync. Single-tool repetition is already capped by
            # the per-tool loop guard, so this guard is only needed for
            # multi-call batch replay.
            if parsed_calls and len(parsed_calls) >= 2:
                _batch_fp = json.dumps(
                    sorted(
                        (c["tool_name"], json.dumps(c.get("args") or {}, sort_keys=True))
                        for c in parsed_calls
                    ),
                    sort_keys=True,
                )
                _batch_fp_counts[_batch_fp] = _batch_fp_counts.get(_batch_fp, 0) + 1
                if _batch_fp_counts[_batch_fp] >= 3:
                    logger.warning(
                        "v3 stream: batch-replay guard tripped (conv=%s, iter=%d, "
                        "batch=%d calls, fp=%.120s) — breaking loop",
                        conversation_id, iteration, len(parsed_calls), _batch_fp,
                    )
                    llm_messages.append({
                        "role": "user",
                        "content": (
                            "You have re-emitted the exact same tool-call batch "
                            "multiple times. Stop repeating it. Use the results "
                            "you already have and produce your final answer now."
                        ),
                    })
                    break

            # ── Dashboard guard interception ──────────────────────────
            # The guard above set tool_choice to force create_dashboard, but
            # weaker models (e.g. qwen3.5-27b) often ignore that and return
            # execute_query instead. Block those calls AND inject a synthetic
            # tool result so the LLM sees why its call was rejected and
            # retries with create_dashboard. Only `create_dashboard` and
            # harmless side-effect tools are allowed through.
            # ── Data-contract confirmation gate (T7) ──────────────────
            # If the LLM tries to call the build tool while the data contract
            # is unconfirmed (ambiguous request, no schema grounding, no user
            # approval), block the build and inject a synthetic clarification
            # so the agent asks instead of building on invented tables.
            if parsed_calls and _dash_build_tool in {p["tool_name"] for p in parsed_calls}:
                if contract_confirmation_needed(user_content, tool_calls_for_frontend):
                    _build_call = next(
                        (p for p in parsed_calls if p["tool_name"] == _dash_build_tool),
                        None,
                    )
                    _non_build = [p for p in parsed_calls if p["tool_name"] != _dash_build_tool]
                    if _build_call is not None:
                        logger.warning(
                            "v3 stream: data-contract gate blocked %s on unconfirmed "
                            "contract (conv=%s, iter=%d).",
                            _dash_build_tool, conversation_id, iteration,
                        )
                        llm_messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": _build_call["tool_call_id"],
                                "type": "function",
                                "function": {
                                    "name": _dash_build_tool,
                                    "arguments": _build_call["args_str"],
                                },
                            }],
                        })
                        llm_messages.append({
                            "role": "tool",
                            "tool_call_id": _build_call["tool_call_id"],
                            "content": (
                                "BLOCKED by the data-contract guard: you tried to build a "
                                "live dashboard before confirming the data contract. You "
                                "MUST first inspect the real schema with `describe_schema` "
                                "or `inspect_data_source`. If the user's request is "
                                "ambiguous about which table, metric, or aggregation to "
                                "use, ask the user ONE clarifying question and wait for "
                                "the answer. NEVER invent table or column names — a "
                                "dashboard built on fabricated data is worse than none."
                            ),
                        })
                        tool_calls_for_frontend.append({
                            "id": f"contract_gate_{uuid.uuid4()}",
                            "name": "data_contract_gate",
                            "status": "blocked",
                            "results": {
                                "blocked": _dash_build_tool,
                                "reason": "data contract not confirmed (ambiguous request, no schema grounding)",
                            },
                        })
                        parsed_calls = _non_build
                        if not _non_build:
                            yield (f'data: {json.dumps({"type": "tool_call", "name": "data_contract_gate", "args": {"blocked": _dash_build_tool}})}\n\n')
                            continue

            # ── Deliverable phase-lock gate (Bug 1/2 fix) ───────────────
            # The model must NOT build the deliverable (create_artifact /
            # run_sandbox_skill) before answer-tagged data has been
            # collected. When the contract requires data and no answer
            # dataset exists yet, block the build call and inject a
            # synthetic tool result steering the agent to query first.
            if (
                settings.DELIVERABLE_PHASE_LOCK_ENABLED
                and _contract is not None
                and _contract.requires_data
                and not _contract.has_answer_data()
            ):
                _phase_locked_calls = [
                    p for p in parsed_calls
                    if _phase_lock_should_block(p)
                ]
                if _phase_locked_calls:
                    _phase_blocked_ids = {
                        p["tool_call_id"] for p in _phase_locked_calls
                    }
                    _non_phase_locked = [
                        p for p in parsed_calls
                        if p["tool_call_id"] not in _phase_blocked_ids
                    ]
                    for _plc in _phase_locked_calls:
                        logger.warning(
                            "v3 stream: deliverable phase-lock blocked %s before "
                            "data collection (conv=%s, iter=%d)",
                            _plc["tool_name"], conversation_id, iteration,
                        )
                        llm_messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": _plc["tool_call_id"],
                                "type": "function",
                                "function": {
                                    "name": _plc["tool_name"],
                                    "arguments": _plc["args_str"],
                                },
                            }],
                        })
                        llm_messages.append({
                            "role": "tool",
                            "tool_call_id": _plc["tool_call_id"],
                            "content": (
                                "BLOCKED by the deliverable phase-lock: you tried to "
                                "build the deliverable before collecting the data it "
                                "needs. Query the data first (ask_data_agent / "
                                "execute_query), WAIT for the returned rows, then build "
                                "the deliverable from those rows. Never fabricate data "
                                "or build on empty results."
                            ),
                        })
                        tool_calls_for_frontend.append({
                            "id": f"deliverable_gate_{uuid.uuid4()}",
                            "name": "deliverable_phase_lock",
                            "status": "blocked",
                            "results": {
                                "blocked": _plc["tool_name"],
                                "reason": "deliverable requires data that has not been collected yet",
                            },
                        })
                    parsed_calls = _non_phase_locked
                    if not _non_phase_locked:
                        yield (
                            f'data: {json.dumps({"type": "tool_call", "name": "deliverable_phase_lock", "args": {"blocked": [p["tool_name"] for p in _phase_locked_calls]}})}\n\n'
                        )
                        continue

            # ── T12: describe_schema per-turn cap (dashboard turns) ──────
            # Re-inspecting the schema costs a whole tool iteration each time
            # AND widens the soft budget via the edge-discovery hook, letting
            # the agent explore forever instead of shipping the dashboard.
            # Once the cap is reached, block further describe_schema calls,
            # freeze the budget-upgrade hook, and steer the model to
            # create_fullstack_dashboard NOW.
            _max_describe_cap = getattr(
                settings, "MAX_DESCRIBE_SCHEMA_PER_DASHBOARD_TURN", 2
            )
            if (
                _max_describe_cap > 0
                and any(
                    p["tool_name"] in DASHBOARD_SCHEMA_CAP_TOOLS
                    for p in parsed_calls
                )
                and describe_schema_cap_reached(
                    user_content,
                    _v3_executed_tool_names,
                    _max_describe_cap,
                )
            ):
                _cap_blocked = [
                    p for p in parsed_calls
                    if p["tool_name"] in DASHBOARD_SCHEMA_CAP_TOOLS
                ]
                _schema_budget_frozen = True
                logger.warning(
                    "v3 stream: describe_schema cap reached (%d/%d) for "
                    "dashboard turn (conv=%s, iter=%d); blocking %d further "
                    "describe_schema call(s) and freezing budget upgrade.",
                    _describe_schema_count, _max_describe_cap,
                    conversation_id, iteration, len(_cap_blocked),
                )
                for _blk in _cap_blocked:
                    llm_messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": _blk["tool_call_id"],
                            "type": "function",
                            "function": {
                                "name": _blk["tool_name"],
                                "arguments": _blk["args_str"],
                            },
                        }],
                    })
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": _blk["tool_call_id"],
                        "content": (
                            "BLOCKED: the schema has already been inspected "
                            f"{_describe_schema_count} times this turn (cap "
                            f"{_max_describe_cap}). STOP exploring the schema "
                            "— you have enough information to build the "
                            f"dashboard. Call {_dash_build_tool} NOW with the "
                            "widgets you have planned, even if you only have a "
                            "few columns. The dashboard renders inline in the "
                            "chat."
                        ),
                    })
                    tool_calls_for_frontend.append({
                        "id": f"schema_cap_{uuid.uuid4()}",
                        "name": "describe_schema_cap",
                        "status": "blocked",
                        "results": {
                            "blocked": _blk["tool_name"],
                            "reason": (
                                f"describe_schema called "
                                f"{_describe_schema_count} times "
                                f"(cap {_max_describe_cap}); build now"
                            ),
                        },
                    })
                parsed_calls = [
                    p for p in parsed_calls
                    if p["tool_name"] not in DASHBOARD_SCHEMA_CAP_TOOLS
                ]
                if not parsed_calls:
                    tool_calls_for_frontend.append({
                        "id": f"schema_cap_redirect_{uuid.uuid4()}",
                        "name": "describe_schema_cap",
                        "status": "blocked",
                        "results": {
                            "blocked": "all_calls",
                            "reason": "cap reached; redirect to build tool",
                        },
                    })
                    yield (f'data: {json.dumps({"type": "tool_call", "name": "describe_schema_cap", "args": {"blocked": "describe_schema"}})}\n\n')
                    continue

            # ── Fix 2: hard-block anti-tools on dashboard turns ──────────
            # Before the full-stack build tool has run, create_artifact and
            # the legacy create_dashboard are bypasses that silently ship a
            # static page instead of the real-time dashboard. Block them and
            # nudge the model to call the build tool NOW with the queries it
            # already ran. The ACTIVE build tool itself is never blocked (in
            # legacy mode create_dashboard IS the build tool).
            if dashboard_antitools_should_block(
                user_content, _v3_executed_tool_names
            ):
                _anti_blocked = [
                    p for p in parsed_calls
                    if p["tool_name"] in DASHBOARD_ANTITOOLS
                    and p["tool_name"] != _dash_build_tool
                ]
                if _anti_blocked:
                    logger.warning(
                        "v3 stream: dashboard anti-tools blocked %s on "
                        "dashboard turn (conv=%s, iter=%d); steering to %s.",
                        [b["tool_name"] for b in _anti_blocked],
                        conversation_id, iteration, _dash_build_tool,
                    )
                    for _blk in _anti_blocked:
                        llm_messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": _blk["tool_call_id"],
                                "type": "function",
                                "function": {
                                    "name": _blk["tool_name"],
                                    "arguments": _blk["args_str"],
                                },
                            }],
                        })
                        llm_messages.append({
                            "role": "tool",
                            "tool_call_id": _blk["tool_call_id"],
                            "content": (
                                "BLOCKED: this turn is a dashboard build. "
                                "Static HTML artifacts and the legacy "
                                f"dashboard tool are disabled here. Call "
                                f"{_dash_build_tool} NOW with the queries and "
                                "schema you already gathered — the dashboard "
                                "renders inline in the chat."
                            ),
                        })
                        tool_calls_for_frontend.append({
                            "id": f"antitool_{uuid.uuid4()}",
                            "name": "dashboard_antitool",
                            "status": "blocked",
                            "results": {
                                "blocked": _blk["tool_name"],
                                "reason": (
                                    "static/legacy dashboard build blocked; "
                                    "use create_fullstack_dashboard"
                                ),
                            },
                        })
                    _anti_blocked_names = {b["tool_name"] for b in _anti_blocked}
                    parsed_calls = [
                        p for p in parsed_calls
                        if p["tool_name"] not in _anti_blocked_names
                    ]
                    if not parsed_calls:
                        tool_calls_for_frontend.append({
                            "id": f"antitool_redirect_{uuid.uuid4()}",
                            "name": "dashboard_antitool",
                            "status": "blocked",
                            "results": {
                                "blocked": "all_calls",
                                "reason": "anti-tool blocked; redirect to build tool",
                            },
                        })
                        yield (f'data: {json.dumps({"type": "tool_call", "name": "dashboard_antitool", "args": {"blocked": "anti_tools"}})}\n\n')
                        continue

            # ── P1-4b: hard delegation nudge (2026-08-29) ────────────────
            # The system-prompt directive is soft — weak models ignore it and
            # answer "top 5 customers, top 5 products, top 3 regions"
            # linearly (observed: 12 tool calls → "trouble putting it all
            # together"). After one iteration of linear exploration, inject a
            # hard user-role instruction ONCE (capped) steering to
            # delegate_task. Mirrors the dashboard narration-nudge pattern.
            if not _delegation_nudged and iteration >= 0:
                try:
                    from app.services.delegation_nudge import delegation_nudge_directive

                    _del_hard = delegation_nudge_directive(user_content)
                except Exception:  # noqa: BLE001
                    _del_hard = None
                if (
                    _del_hard
                    and _dash_build_tool not in _v3_executed_tool_names
                    and "delegate_task" not in _v3_executed_tool_names
                ):
                    _delegation_nudged = True
                    _del_msg = (
                        "SYSTEM GUARDRAIL: the user asked for multiple "
                        "INDEPENDENT items (top-N lists / comparisons). "
                        "Stop answering them one-by-one. Call "
                        "`delegate_task(tasks=[...])` NOW with ONE task string "
                        "per item so they run as parallel sub-agents, then "
                        "synthesize their results. Every item must be answered."
                    )
                    llm_messages.append({"role": "user", "content": _del_msg})
                    logger.info(
                        "v3 stream: hard delegation nudge injected (conv=%s, iter=%d)",
                        conversation_id, iteration,
                    )

            # ── Fix 3: total-exploration cap (dashboard turns) ────────────
            # describe_schema + execute_query + execute_sql + sql_query
            # combined. Fires independently of the T12 describe_schema cap:
            # whichever trips first blocks further exploration, freezes the
            # budget-upgrade hook, and steers the model to the build tool.
            _max_explore = getattr(
                settings, "MAX_DASHBOARD_EXPLORATION_PER_TURN", 8
            )
            if (
                _max_explore > 0
                and any(
                    p["tool_name"] in DASHBOARD_EXPLORATION_TOOLS
                    for p in parsed_calls
                )
                and dashboard_exploration_cap_reached(
                    user_content,
                    _v3_executed_tool_names,
                    _max_explore,
                )
            ):
                _explore_blocked = [
                    p for p in parsed_calls
                    if p["tool_name"] in DASHBOARD_EXPLORATION_TOOLS
                ]
                _schema_budget_frozen = True
                logger.warning(
                    "v3 stream: dashboard exploration cap reached (%d/%d) for "
                    "dashboard turn (conv=%s, iter=%d); blocking %d further "
                    "exploration call(s) and freezing budget upgrade.",
                    len(_v3_executed_tool_names), _max_explore,
                    conversation_id, iteration, len(_explore_blocked),
                )
                for _blk in _explore_blocked:
                    llm_messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": _blk["tool_call_id"],
                            "type": "function",
                            "function": {
                                "name": _blk["tool_name"],
                                "arguments": _blk["args_str"],
                            },
                        }],
                    })
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": _blk["tool_call_id"],
                        "content": (
                            "BLOCKED: total schema exploration + query "
                            f"execution has hit the cap of {_max_explore} "
                            "this turn. STOP exploring — you have enough "
                            f"information. Call {_dash_build_tool} NOW with "
                            "the widgets you have planned."
                        ),
                    })
                    tool_calls_for_frontend.append({
                        "id": f"explore_cap_{uuid.uuid4()}",
                        "name": "dashboard_exploration_cap",
                        "status": "blocked",
                        "results": {
                            "blocked": _blk["tool_name"],
                            "reason": (
                                "total exploration cap "
                                f"({_max_explore}) reached; build now"
                            ),
                        },
                    })
                parsed_calls = [
                    p for p in parsed_calls
                    if p["tool_name"] not in DASHBOARD_EXPLORATION_TOOLS
                ]
                if not parsed_calls:
                    tool_calls_for_frontend.append({
                        "id": f"explore_cap_redirect_{uuid.uuid4()}",
                        "name": "dashboard_exploration_cap",
                        "status": "blocked",
                        "results": {
                            "blocked": "all_calls",
                            "reason": "exploration cap reached; redirect to build tool",
                        },
                    })
                    yield (f'data: {json.dumps({"type": "tool_call", "name": "dashboard_exploration_cap", "args": {"blocked": "exploration"}})}\n\n')
                    continue

            # ── Fix 4: duplicate create_artifact titles (all turns) ───────
            # A second create_artifact call with the same title in one turn is
            # always waste (duplicate static pages). Applies to ALL turns.
            # Titles are recorded here (check-then-add) because every
            # create_artifact remaining in parsed_calls is destined to run.
            _dup_blocked = []
            for _p in parsed_calls:
                if _p["tool_name"] != "create_artifact":
                    continue
                _art_title = parse_artifact_title(_p.get("args_str"))
                if not _art_title:
                    continue
                if _art_title in _create_artifact_titles:
                    _dup_blocked.append(_p)
                else:
                    _create_artifact_titles.add(_art_title)
            if _dup_blocked:
                logger.warning(
                    "v3 stream: duplicate create_artifact title(s) blocked "
                    "(conv=%s, iter=%d): %s",
                    conversation_id, iteration,
                    [b.get("args_str", "") for b in _dup_blocked],
                )
                for _blk in _dup_blocked:
                    llm_messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": _blk["tool_call_id"],
                            "type": "function",
                            "function": {
                                "name": _blk["tool_name"],
                                "arguments": _blk["args_str"],
                            },
                        }],
                    })
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": _blk["tool_call_id"],
                        "content": (
                            "BLOCKED: an artifact with this title already "
                            "exists this turn. Duplicate static artifacts are "
                            "not created — update or iterate on the existing "
                            "one instead."
                        ),
                    })
                    tool_calls_for_frontend.append({
                        "id": f"dup_artifact_{uuid.uuid4()}",
                        "name": "create_artifact_dedup",
                        "status": "blocked",
                        "results": {
                            "blocked": "create_artifact",
                            "reason": "duplicate artifact title this turn",
                        },
                    })
                _dup_ids = {id(b) for b in _dup_blocked}
                parsed_calls = [
                    p for p in parsed_calls if id(p) not in _dup_ids
                ]
                if not parsed_calls:
                    tool_calls_for_frontend.append({
                        "id": f"dup_artifact_redirect_{uuid.uuid4()}",
                        "name": "create_artifact_dedup",
                        "status": "blocked",
                        "results": {
                            "blocked": "all_calls",
                            "reason": "duplicate artifact blocked",
                        },
                    })
                    yield (f'data: {json.dumps({"type": "tool_call", "name": "create_artifact_dedup", "args": {"blocked": "duplicate_artifact"}})}\n\n')
                    continue

            if parsed_calls:
                _dash_guard_names = {p["tool_name"] for p in parsed_calls}
                if dashboard_guard_should_block_queries(
                    _dash_guard_names, _dash_build_tool, dashboard_forced,
                ):
                    # Strip the blocked tool calls; keep only create_dashboard
                    # and harmless tools.
                    _blocked_names = [
                        p["tool_name"]
                        for p in parsed_calls
                        if p["tool_name"] in dashboard_guard_blocked_tools()
                    ]
                    logger.warning(
                        "v3 stream: dashboard guard intercepted %s after "
                        "schema/design pass (conv=%s, iter=%d). Blocking "
                        "those calls; will retry with create_dashboard.",
                        _blocked_names, conversation_id, iteration,
                    )
                    # Inject synthetic tool results for the blocked calls so
                    # the LLM has a complete turn to reason about. Then
                    # retry: keep only the allowed calls for this iteration.
                    _blocked = [p for p in parsed_calls if p["tool_name"] in dashboard_guard_blocked_tools()]
                    _allowed = [p for p in parsed_calls if p["tool_name"] not in dashboard_guard_blocked_tools()]
                    _blocked_summary = ", ".join(sorted({p["tool_name"] for p in _blocked}))
                    for _blk in _blocked:
                        llm_messages.append({
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [{
                                "id": _blk["tool_call_id"],
                                "type": "function",
                                "function": {
                                    "name": _blk["tool_name"],
                                    "arguments": _blk["args_str"],
                                },
                            }],
                        })
                        llm_messages.append({
                            "role": "tool",
                            "tool_call_id": _blk["tool_call_id"],
                            "content": (
                                "BLOCKED by dashboard guard: the user asked "
                                "for a live dashboard. You already have the "
                                "schema + design pass done. STOP running "
                                f"more {_blk['tool_name']} calls. Call "
                                "create_dashboard NOW with the widgets "
                                "you've planned, even if you only have a "
                                "few columns. The dashboard will render "
                                "inline in the chat."
                            ),
                        })
                    if not _allowed:
                        # Nothing else to execute — force a retry that
                        # should land on create_dashboard.
                        tool_calls_for_frontend.append({
                            "id": f"guard_blocked_{uuid.uuid4()}",
                            "name": "dashboard_guard_intercept",
                            "status": "blocked",
                            "results": {
                                "blocked": _blocked_summary,
                                "reason": "guard: schema/design pass done; only create_dashboard allowed",
                            },
                        })
                        yield f'data: {json.dumps({"type": "tool_call", "name": "dashboard_guard_intercept", "args": {"blocked": _blocked_summary}})}\n\n'
                        continue
                    parsed_calls = _allowed

            # Intercept path (R5): if the LLM's batch includes a
            # `create_agent` call, intercept it BEFORE execution, drop the
            # entry from the parallel-execute set, and pause for the
            # Decision Summary card. Siblings still execute normally so
            # their results feed back into the next iteration if the user
            # clicks Cancel.
            intercepted, intercept_payload, intercept_index = _intercept_create_agent(parsed_calls)
            if intercepted:
                logger.info(
                    "create_agent intercept (v3 stream): conv=%s tool_call_id=%s "
                    "payload_keys=%s siblings=%d",
                    conversation_id,
                    parsed_calls[intercept_index]["tool_call_id"],
                    sorted((intercept_payload or {}).keys()),
                    len(parsed_calls) - 1,
                )
                sibling_calls = [c for i, c in enumerate(parsed_calls) if i != intercept_index]
                ctx_v3 = {
                    "conversation_id": conversation_id,
                    "agent_app_id": agent_app_id,
                    "agent_name": agent_name,
                    "conversation_metadata": conv.metadata_ or {},
                    "chat_session_id": chat_session_id,
                    **(data_ctx_extras or {}),
                    "main_agent_will_synthesize": bool(_orch_doc_format),
                }
                sibling_results: list[dict] = []
                if sibling_calls:
                    if len(sibling_calls) == 1:
                        sibling_results = [await execute_tool(
                            sibling_calls[0]["tool_name"],
                            sibling_calls[0]["args"],
                            db,
                            user.id if user else None,
                            context=ctx_v3,
                        )]
                    else:
                        async def _exec_v3_sibling(call):
                            return await execute_tool(
                                call["tool_name"], call["args"], db,
                                user.id if user else None,
                                context=ctx_v3,
                            )
                        raw_sib = await asyncio.gather(
                            *[_exec_v3_sibling(c) for c in sibling_calls],
                            return_exceptions=True,
                        )
                        for i, r in enumerate(raw_sib):
                            if isinstance(r, Exception):
                                logger.warning("sibling tool '%s' raised: %s", sibling_calls[i]["tool_name"], r)
                                sibling_results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
                            else:
                                sibling_results.append(r)
                # Record siblings in the assistant message + LLM trace.
                for sib_call, sib_result in zip(sibling_calls, sibling_results):
                    sib_name = sib_call["tool_name"]
                    sib_display = TOOL_DISPLAY_NAMES.get(sib_name, sib_name)
                    sib_record = {
                        "id": sib_call["tool_call_id"],
                        "name": sib_display,
                        "arguments_string": sib_call["args_str"],
                        "results": sib_result,
                        "status": "completed" if isinstance(sib_result, dict) and sib_result.get("success") else "failed",
                    }
                    if sib_name in INTERNAL_TOOLS:
                        sib_record["display_projection"] = _internal_tool_projection(sib_name)
                    tool_calls_for_frontend.append(sib_record)
                    # P0: incremental trace_step — reuses the same step shape
                    # as _derive_trace_from_response so the frontend
                    # ReasoningSummary component needs no schema change.
                    display_proj = sib_record.get("display_projection") or {}
                    if display_proj.get("hide_details"):
                        _step_title = display_proj.get("label", sib_record.get("name", ""))
                        _step_detail = display_proj.get("done_label", "")
                    else:
                        _step_title = sib_record.get("name", "")
                        _results = sib_record.get("results")
                        if isinstance(_results, dict):
                            _step_detail = str(_results.get("summary") or _results.get("text") or "")[:200]
                        elif isinstance(_results, str):
                            _step_detail = _results[:200]
                        else:
                            _step_detail = ""
                    _step = {
                        "step": len(tool_calls_for_frontend),
                        "type": "tool_call",
                        "title": _step_title,
                        "detail": _step_detail,
                        "status": sib_record.get("status", "completed"),
                        "duration_ms": int(sib_record.get("duration_ms") or 0),
                    }
                    yield f'data: {json.dumps({"type": "trace_step", "step": _step})}\n\n'
                    llm_messages.append({
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{"id": sib_call["tool_call_id"], "type": "function",
                                        "function": {"name": sib_name, "arguments": sib_call["args_str"]}}],
                    })
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": sib_call["tool_call_id"],
                        "content": json.dumps(sib_result),
                    })
                # ReAct reflexion: if any sibling tool failed, inject a
                # critique system message before the next LLM iteration.
                _inject_reflexion_critique(llm_messages, sibling_calls, sibling_results)
                # Now mark the create_agent entry as awaiting_decision_summary.
                intercepted_call = parsed_calls[intercept_index]
                intercepted_record = {
                    "id": intercepted_call["tool_call_id"],
                    "name": TOOL_DISPLAY_NAMES.get("create_agent", "create_agent"),
                    "arguments_string": intercepted_call["args_str"],
                    "status": "awaiting_decision_summary",
                }
                tool_calls_for_frontend.append(intercepted_record)
                # Persist pause + emit SSE.
                paused, stripped_for_ui, _note = _persist_decision_summary_pause(
                    db, conv, messages, assistant_msg_id,
                    tool_calls_for_frontend, assistant_content,
                    tool_call_payload=intercept_payload,
                )
                if paused:
                    # Stream the assistant's narrative prose first so the user
                    # sees the agent's recommendation.
                    if stripped_for_ui:
                        yield f'data: {json.dumps({"type": "delta", "content": stripped_for_ui})}\n\n'
                    # Refresh so the SSE payload reflects the new metadata.
                    try:
                        conv = db.query(AgentConversation).filter(
                            AgentConversation.id == conversation_id,
                        ).first() or conv
                    except Exception:
                        pass
                yield f'data: {json.dumps({"type": "paused", "reason": "awaiting_decision_summary", "conversation": conv.to_dict()})}\n\n'
                _discard_steer(conversation_id)
                return
                # Sanitiser rejected — fall through to normal flow. Rare.

            # ── Activity step: emit "running" for each tool ──
            # Phase headline: the first tool batch flips the turn into
            # "Fabricating" (Claude-style: the headline names the moment).
            if "act" not in _emitted_phases:
                _emitted_phases.add("act")
                yield _emit_phase("act")
                # Live feed: the turn is now proven tool-bound — open the
                # feed with the goal headline, then the act headline.
                _le_goal = _push_live_event("phase_enter", "phase_enter.goal")
                if _le_goal:
                    yield _le_goal
                _le_act = _push_live_event("phase_enter", "phase_enter.act")
                if _le_act:
                    yield _le_act

            _tool_step_indices: list[int] = []
            # Count parallel calls per tool for index suffix
            _tool_counts: dict[str, int] = {}
            # 2026-08-25: stack of open subagents (by id). Used to set
            # parent_subagent_id on the tool_call_started event so the
            # frontend can nest tool rows under their subagent parent.
            # Each entry is {"id": str, "target": str} — id is a UUID-like
            # string the frontend uses as a stable React key.
            _subagent_id_stack: list[dict] = []
            _subagent_seq: int = 0
            for pc in parsed_calls:
                _tool_counts[pc["tool_name"]] = _tool_counts.get(pc["tool_name"], 0) + 1
            _tool_run_index: dict[str, int] = {}
            for pc in parsed_calls:
                _step_counter[0] += 1
                desc = _format_activity_description(pc["tool_name"], pc.get("args"))
                # Append (N/M) suffix for parallel same-tool calls
                tn = pc["tool_name"]
                idx = _tool_run_index.get(tn, 0) + 1
                _tool_run_index[tn] = idx
                total = _tool_counts.get(tn, 1)
                if total >= 2:
                    desc = f"{desc} ({idx}/{total})"
                cmd = _summarize_tool_command(tn, pc.get("args"))
                yield _emit_activity_step(_step_counter[0], desc, "running", tool_name=tn, command=cmd)
                _step_start_times[_step_counter[0]] = time.monotonic()
                _le_tool = _push_live_event("tool_call_started", "tool_call_started", {"tool_label": desc})
                # 2026-08-25: if there's an open subagent with matching target,
                # set parent_subagent_id so the frontend nests this tool row
                # under the subagent row (eliminates the duplicate-row noise).
                _parent_sub_id = None
                for _sa in reversed(_subagent_id_stack):
                    if _sa["target"] == tn:
                        _parent_sub_id = _sa["id"]
                        break
                if _parent_sub_id:
                    # Re-push with the parent field. _push_live_event returns
                    # a frame string we can't mutate, so we append the field
                    # via a new event variant: "tool_call_started_parent".
                    # Actually simpler: include it in the params dict so it
                    # travels with the event.
                    pass  # parent_subagent_id is now added below
                if _le_tool and _parent_sub_id:
                    # Inject the parent_subagent_id into the existing frame
                    # by re-serializing with the extra field. This is cheap
                    # and avoids a duplicate event.
                    _le_tool = _le_tool.rstrip("\n\n").rstrip()
                    try:
                        _frame = json.loads(_le_tool[len("data: "):])
                        _frame.setdefault("params", {})["parent_subagent_id"] = _parent_sub_id
                        _le_tool = f"data: {json.dumps(_frame)}\n\n"
                    except Exception:
                        pass
                if _le_tool:
                    yield _le_tool
                # 2026-08-25: live-streaming spec — emit search_query_delta for search-style tools.
                # Extracts the query from the tool args and simulates a typing effect
                # so the UI can show the query being built character-by-character.
                _SEARCH_TOOL_NAMES = {
                    "web_search", "_web_search", "tavily_search",
                    "ask_rag_research", "_ask_rag_research", "rag_research",
                    "search_documents", "_search_documents",
                }
                if tn in _SEARCH_TOOL_NAMES:
                    try:
                        from app.services.agent_loop.streaming_helpers import _stream_typing_effect
                        _q = (pc.get("args", {}).get("query")
                              or pc.get("args", {}).get("question")
                              or pc.get("args", {}).get("q")
                              or "")
                        if _q:
                            _tc_id = f"tc-{_step_counter[0]}"
                            async for _typing_frame in _stream_typing_effect(str(_q), _tc_id):
                                yield _typing_frame
                    except Exception as _typing_err:
                        logger.debug("typing effect failed (non-fatal): %s", _typing_err)
                # Sub-agent delegation badge: surfaces what the parent chat
                # loop asked the sub-agent to do (e.g. ask_data_agent).
                if tn.startswith("ask_"):
                    _subagent_seq += 1
                    _subagent_id = f"sub_{_subagent_seq}"
                    _le_sub_inv = _push_live_event(
                        "subagent_invoked", "subagent_invoked",
                        {"agent_label": desc, "target": tn, "subagent_id": _subagent_id},
                    )
                    if _le_sub_inv:
                        yield _le_sub_inv
                    # Push to the open-subagent stack so the next
                    # tool_call_started can pick this up as its parent.
                    _subagent_id_stack.append({"id": _subagent_id, "target": tn})
                _activity_steps.append({
                    "number": _step_counter[0],
                    "description": desc,
                    "status": "running",
                    "tool_name": tn,
                    **({"command": cmd} if cmd else {}),
                })
                _tool_step_indices.append(_step_counter[0])

            ctx = {
                "conversation_id": conversation_id,
                "agent_app_id": agent_app_id,
                "agent_name": agent_name,
                "conversation_metadata": conv.metadata_ or {},
                "chat_session_id": chat_session_id,
                **(data_ctx_extras or {}),
                "endpoint": effective_llm.endpoint,
                # PERF 2026-08-24: deliverable turns synthesize post-loop;
                # the data agent can skip its narrate LLM call (saves ~1 call
                # per ask_data_agent). Simple Q&A turns keep narrate.
                "main_agent_will_synthesize": bool(_orch_doc_format),
                # 2026-08-27: opt-in progress sub-steps for ask_data_agent.
                # The handler appends SSE activity_step strings into this list;
                # we drain+yield it right after the tool batch finishes so the
                # UI can show "Resolving schema → Generating SQL → Executing".
                "progress_emitter": _tool_progress,
                "step_counter": _step_counter,
            }

            # ── Tool execution ──────────────────────────────────────────────
            # Long-running delegation tools (ask_perception / ask_intelligence
            # / ask_diagnosis / batch tool / …) can block for minutes. When
            # any call in this batch is long-running, run the whole batch in a
            # background task and emit ``tool_progress`` heartbeat frames while
            # it executes, so the client sees liveness and proxies don't
            # idle-kill the connection.
            async def _run_tool_batch():
                # P2-12: shared core in app.services.agent_loop.tool_executor.
                async def _invoke_v3(tool_name, args):
                    nonlocal _fusion_clear_count  # loop-scope counter; += below would otherwise shadow it locally
                    nonlocal _fusion_readonly_streak
                    if _is_dashboard_build and tool_name == "ask_data_agent":
                        # Dashboard builds collect metrics across many tables;
                        # give the data-agent delegate the dashboard budget
                        # (10 min) instead of the default 60s truncation so
                        # every metric is gathered before the build starts.
                        args = {**(args or {}), "budget_seconds": settings.DASHBOARD_DELEGATE_BUDGET_SECONDS}
                    # CAD destructive-reclear guard (2026-08-27, hardened
                    # 2026-08-28): a modeling tool already succeeded THIS turn,
                    # so `fusion360_clear` would wipe the model mid-build (the
                    # classic build-head → re-clear → rebuild → fail pattern),
                    # and a SECOND clear in one turn always means the model is
                    # re-planning from scratch instead of continuing (observed
                    # 2026-08-28: clear re-emitted at the head of every batch,
                    # 4× in one turn, wiping sketches it had just created).
                    # Block the clear with an actionable synthetic result.
                    if (
                        _is_fusion_agent
                        and tool_name == "fusion360_clear"
                        and (
                            _fusion_clear_count >= 1
                            or _fusion_build_progress_this_turn(tool_calls_for_frontend)
                        )
                    ):
                        logger.warning(
                            "v3 stream: blocked destructive fusion360_clear mid-build "
                            "(conv=%s, iter=%d, clear_count=%d)",
                            conversation_id, iteration, _fusion_clear_count,
                        )
                        return {
                            "success": False,
                            "error": (
                                "BLOCKED: the Fusion scene was already cleared this "
                                "turn — do NOT clear again, it would wipe work in "
                                "progress and restart from zero. Continue from the "
                                "current scene: call fusion360_info to re-sync the "
                                "live sketch/body indices, then keep building. Only "
                                "an explicit user request for a brand-new model "
                                "justifies a clear, and then only once per turn."
                            ),
                            "guardrail": {"code": "fusion_destructive_reclear_block"},
                        }
                    # ── Read-only spin guard (2026-08-28) ──────────────
                    # Local models occasionally loop on re-sync: "I'll re-read
                    # the live scene" → fusion360_info, forever (observed 10×
                    # in one turn). Count CONSECUTIVE read-only calls (any
                    # modeling tool resets the streak) and hard-block after 5
                    # so the model is forced to either modify geometry or
                    # answer. Legit info-after-modify is unaffected.
                    _FUSION_READONLY = {"fusion360_info", "fusion360_ping",
                                        "fusion360_project", "fusion360_lookup_api"}
                    if _is_fusion_agent and tool_name in _FUSION_READONLY:
                        _fusion_readonly_streak += 1
                    elif (
                        _is_fusion_agent
                        and tool_name.startswith("fusion360_")
                        and tool_name not in _FUSION_READONLY
                    ):
                        # A real modeling tool changes geometry — the streak
                        # resets so info-after-modify stays legal. todo/plan
                        # calls do NOT reset it (they don't touch geometry).
                        _fusion_readonly_streak = 0
                    if (
                        _is_fusion_agent
                        and tool_name in _FUSION_READONLY
                        # 2026-08-28: 5 → 3. deepseek re-reads info 3-5x at
                        # turn start before acting; each wasted re-read costs a
                        # full LLM round trip (~5-15s on api.deepseek.com).
                        # Blocking at 3 saves ~2 calls per turn while still
                        # allowing one legit re-sync after a modeling change.
                        and _fusion_readonly_streak >= 3
                    ):
                        logger.warning(
                            "v3 stream: blocked read-only spin (conv=%s, iter=%d, "
                            "tool=%s, streak=%d)",
                            conversation_id, iteration, tool_name,
                            _fusion_readonly_streak,
                        )
                        return {
                            "success": False,
                            "error": (
                                "BLOCKED: you have re-read the Fusion scene %d "
                                "times in a row WITHOUT changing any geometry. "
                                "The scene has NOT changed. Use the "
                                "fusion360_info result you already have and call "
                                "a MODELING tool now (fusion360_sketch_create / "
                                "fusion360_extrude / fusion360_fillet / "
                                "fusion360_thread ...), or give your final "
                                "answer. Do NOT call fusion360_info again."
                            ) % _fusion_readonly_streak,
                            "guardrail": {"code": "fusion_readonly_spin_block"},
                        }
                    if tool_name == "fusion360_clear":
                        _fusion_clear_count += 1
                    # Clarify handoff (2026-08-28): the model must not ask a
                    # SECOND question in one turn. The first clarify already
                    # suspends the turn (see the post-batch break above), so
                    # this only fires for same-batch duplicates or when the
                    # suspend flag is off — defense in depth.
                    if (
                        tool_name == "clarify"
                        and _clarify_issued
                        and settings.CLARIFY_SUSPENDS_TURN_ENABLED
                    ):
                        logger.warning(
                            "v3 stream: blocked duplicate clarify (conv=%s, iter=%d)",
                            conversation_id, iteration,
                        )
                        return {
                            "success": False,
                            "error": (
                                "You already asked the user a question this turn — "
                                "STOP and wait for the answer in the next message. "
                                "Do NOT call clarify again."
                            ),
                            "guardrail": {"code": "clarify_duplicate_block"},
                        }
                    return await execute_tool(
                        tool_name, args, db,
                        user.id if user else None, context=ctx,
                    )

                return await execute_tool_batch(
                    parsed_calls,
                    before_call=guard_ctrl.before_call,
                    invoke=_invoke_v3,
                    blocked_result_factory=_guardrail_synthetic_result,
                )

            if any(_is_long_running_tool(c["tool_name"]) for c in parsed_calls):
                _tool_task = asyncio.ensure_future(_run_tool_batch())
                try:
                    async for _frame in _emit_tool_progress_while_waiting(_tool_task, parsed_calls):
                        yield _frame
                finally:
                    if not _tool_task.done():
                        _tool_task.cancel()
                results = _tool_task.result()
            else:
                results = await _run_tool_batch()

            # 2026-08-27: drain any opt-in sub-step progress frames the
            # ask_data_agent handler appended (Resolving schema → Generating
            # SQL → Executing). They are yielded here, after the tool batch
            # returns, so the UI gets granular feedback for slow DB queries.
            for _tp_frame in _tool_progress:
                yield _tp_frame
            _tool_progress.clear()

            # Process results — check for approval pause
            paused_for_approval = False
            pending_tool = None
            for idx, (call, result) in enumerate(zip(parsed_calls, results)):
                tool_name = call["tool_name"]
                args_str = call["args_str"]
                tool_call_id = call["tool_call_id"]
                display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)
                # Clarify handoff (2026-08-28): a successful clarify means the
                # user's next message IS the answer — the turn must suspend.
                # Capture the question so the final bubble shows it instead of
                # the empty "I gathered some information" fallback.
                if (
                    tool_name == "clarify"
                    and isinstance(result, dict)
                    and result.get("success")
                    and settings.CLARIFY_SUSPENDS_TURN_ENABLED
                ):
                    _clarify_issued = True
                    _clarify_question_text = (
                        result.get("question") or _clarify_question_text
                    )
                # T12: count describe_schema executions + record executed tool
                # names (canonical) so the pre-execution interception zone can
                # enforce the per-turn cap on the NEXT iteration.
                _v3_executed_tool_names.append(tool_name)
                # Tier 1 auto-refine tracking (2026-08-28): the build tool's
                # result carries a deterministic `quality` verdict. Capture the
                # worst grade + hard gaps; an update afterwards marks refined.
                if tool_name == _dash_build_tool and isinstance(result, dict) and result.get("success"):
                    _q_res = result.get("quality")
                    if isinstance(_q_res, dict):
                        _q_grade = _q_res.get("grade")
                        if _q_grade in ("B", "C"):
                            _dash_quality_worst = _q_grade
                            _dash_quality_gaps = list(_q_res.get("hard_gaps") or [])
                        elif _q_grade == "A":
                            _dash_quality_worst = "A"
                if tool_name == "update_fullstack_dashboard" and isinstance(result, dict) and result.get("success"):
                    _dash_refined = True
                # Turn-plan evidence (2026-08-27): mark plan steps complete
                # when a tool that evidences them SUCCEEDS, and emit
                # plan_step_completed so the UI checklist ticks off as the
                # agent executes its plan. Deterministic — never trusts model
                # prose. Failed tools do NOT advance the plan.
                if (
                    _turn_plan is not None
                    and _turn_plan.steps
                    and isinstance(result, dict)
                    and result.get("success")
                ):
                    _plan_new_done = plan_completed_steps(
                        _turn_plan, _v3_executed_tool_names,
                    ) - _turn_plan_completed
                    if _plan_new_done:
                        _turn_plan_completed |= _plan_new_done
                        for _plan_idx in sorted(_plan_new_done):
                            _plan_st = _turn_plan.step_by_index(_plan_idx)
                            if _plan_st is not None:
                                yield plan_step_completed_frame(_plan_st)
                                # Persist the completion so the reloaded
                                # checklist shows the tick (parity with the
                                # live stream).
                                _le_done = _push_live_event(
                                    "plan_step_completed", "plan_step_completed",
                                    {"step_index": _plan_st.step_index, "title": _plan_st.title_en},
                                    sanitize=False,
                                )
                                if _le_done:
                                    yield _le_done
                # T19: record FAILED tool executions. `result` is already
                # normalized here — exceptions were converted to
                # {"success": False, "error": ...} by _run_tool_batch, and
                # handlers signal failure via the same shape. Explicitly
                # failed builds must not unlock the orchestrator fallback.
                if isinstance(result, dict) and result.get("success") is False:
                    _v3_failed_tool_names.add(tool_name)
                if tool_name == "describe_schema":
                    _describe_schema_count += 1
                if tool_name == "create_artifact":
                    _art_title = parse_artifact_title(args_str)
                    if _art_title:
                        _create_artifact_titles.add(_art_title)

                if isinstance(result, dict) and result.get("requires_approval"):
                    tool_call_record = {
                        "id": tool_call_id, "name": display_name,
                        "arguments_string": args_str, "results": result,
                        "status": "awaiting_approval",
                        "approval_id": result.get("approval_id"),
                        "reason": result.get("reason", ""),
                    }
                    tool_calls_for_frontend.append(tool_call_record)
                    pending_tool = {
                        "tool_name": tool_name, "args": call["args"],
                        "args_str": args_str, "tool_call_id": tool_call_id,
                        "approval_id": result.get("approval_id"),
                        "remaining_calls": [
                            c for c in parsed_calls[parsed_calls.index(call) + 1:]
                        ],
                    }
                    paused_for_approval = True
                    break

                # ── Hermes smart retry: empty ask_data_agent result ─────
                # The query ran to completion but returned 0 rows (or only
                # metadata-only rows like SELECT COUNT(*)). Before giving
                # the user a "no data" answer, do a Hermes-style re-plan:
                # re-read the FULL warehouse catalog, ask the LLM to pick a
                # DIFFERENT table than the one that returned 0 rows, and
                # re-run ask_data_agent with the revised question. Strict
                # budget: max 2 attempts so a pathological schema can't
                # loop forever. If data comes back, `result` is replaced
                # so the normal data path below synthesizes/finalizes.
                #
                # 2026-08-25: REMOVED the `_orch_doc_format` gate so this
                # fires for ANY data question, not just file-format turns.
                # Previously, "tell me about last month sales" with no file
                # intent would skip this retry and let the agent give up
                # after one wrong-table attempt. Now the agent re-plans
                # with the catalog in hand.
                # FIX 2026-08-24: ALSO retry when the result is metadata-only
                # (e.g. [{row_count: 14275}] from SELECT COUNT(*)) — such
                # results are just as useless as 0 rows for building a
                # deliverable, but the old check (rows == []) let them
                # through to the synthesis pipeline untouched.
                _rows_for_retry_check = result.get("rows") if isinstance(result, dict) else None
                _needs_smart_retry = False
                if (
                    tool_name == "ask_data_agent"
                    and isinstance(result, dict)
                    and _smart_retry_budget[0] > 0
                ):
                    if _rows_for_retry_check == []:
                        _needs_smart_retry = True
                    elif isinstance(_rows_for_retry_check, list) and _rows_for_retry_check:
                        try:
                            # NOTE: uses the module-level import (line ~193).
                            # A local `from … import is_metadata_only_rows`
                            # here shadowed the global for the WHOLE function
                            # and crashed line ~11474 with UnboundLocalError
                            # when this branch never executed.
                            if len(_rows_for_retry_check) <= 2 and is_metadata_only_rows(
                                _rows_for_retry_check
                            ):
                                _needs_smart_retry = True
                                logger.info(
                                    "v3 smart retry triggered: metadata-only rows "
                                    "(%d rows) for conv=%s",
                                    len(_rows_for_retry_check), conversation_id,
                                )
                        except Exception:
                            pass
                if _needs_smart_retry:
                    try:
                        async def _retry_llm_call(_sys, _msgs, _ep=effective_llm.endpoint):
                            _r = await _call_synthesis_llm(_sys, _msgs, endpoint=_ep)
                            return _r.get("content", "") if isinstance(_r, dict) else ""

                        async def _retry_execute(_q):
                            return await execute_tool(
                                "ask_data_agent", {"question": _q},
                                db, user.id if user else None, context=ctx,
                            )

                        # ── Hermes catalog supplier ───────────────────────
                        # Re-reads the bound data source's table-of-contents
                        # fresh before each retry attempt. Injected into the
                        # LLM's revision prompt so it can pick a DIFFERENT
                        # table than the one that returned 0 rows. Failures
                        # here are non-fatal — smart_retry proceeds with just
                        # the failure context (legacy behavior).
                        async def _retry_get_catalog(
                            _dsr_db=db,
                            _agent=agent_app,
                            _pid=_v3_effective_pid,
                            _pname=_v3_effective_pname,
                        ):
                            try:
                                from app.services.data_source_runtime import (
                                    prepare_data_source_runtime,
                                )
                                _rt_db = _dsr_db
                                _opened_locally = False
                                # The v3 stream's db session may be in a
                                # closed/committed state by the time this
                                # retry fires (mid-loop). Open a fresh
                                # session for the catalog read so we never
                                # DetachedInstanceError.
                                try:
                                    from app.database import SessionLocal as _SL
                                    _rt_db = _SL()
                                    _opened_locally = True
                                except Exception:
                                    pass
                                try:
                                    _, _rt_sys, _ = prepare_data_source_runtime(
                                        _rt_db, _agent, [], "",
                                        selected_project_id=_pid,
                                        selected_project_name=_pname,
                                        compact_mode=True,
                                        target_context_window=32_768,
                                    )
                                finally:
                                    if _opened_locally:
                                        try:
                                            _rt_db.close()
                                        except Exception:
                                            pass
                                # Return just the "Bound Data Sources"
                                # section if present, else the whole prompt.
                                if not _rt_sys:
                                    return ""
                                _marker = "Bound Data Sources"
                                _idx = _rt_sys.find(_marker)
                                if _idx >= 0:
                                    return _rt_sys[_idx:][:6000]
                                return _rt_sys[:6000]
                            except Exception as _cat_err:
                                logger.info(
                                    "v3 Hermes catalog fetch failed (non-fatal): %s",
                                    _cat_err,
                                )
                                return ""

                        from app.services.synexia.smart_retry import llm_driven_retry_ask_data
                        _retried = await llm_driven_retry_ask_data(
                            question=user_content,
                            failed_result=result,
                            call_llm_fn=_retry_llm_call,
                            execute_ask_data_fn=_retry_execute,
                            max_attempts=2,
                            get_catalog_fn=_retry_get_catalog,
                        )
                    except Exception as _retry_err:
                        logger.warning(
                            "v3 Hermes smart retry failed (non-fatal) for conv=%s: %s",
                            conversation_id, _retry_err,
                        )
                        _retried = None
                    if _retried is not None and _retried.get("rows"):
                        _smart_retry_budget[0] -= 1
                        result = _retried
                        logger.info(
                            "v3 HERMES RE-PLAN: recovered %d rows for conv=%s "
                            "(question=%s, catalog_used=%s)",
                            len(_retried.get("rows", [])), conversation_id,
                            _retried.get("retried_question", "")[:80],
                            _retried.get("hermes_catalog_used", False),
                        )

                # ── Report synthesis / deferred collection (data tools) ────
                # Any data-producing tool feeds the deferred pipeline:
                # ask_data_agent (delegated), execute_query / execute_sql /
                # sql_query (direct SQL), forecast_brief (market reads).
                # Production bug (2026-08-21): the BI agent collected
                # 41 rows via execute_query, but record_dataset only fired
                # for ask_data_agent → answer_datasets() stayed empty → the
                # post-loop card never built → raw table + no deliverable.
                # The legacy eager-synthesis elif below stays ask_data_agent-
                # only so flag-OFF behavior is unchanged for other tools.
                #
                # 2026-08-25: ``fetch_data_batch`` carries its rows in a
                # nested ``result["results"][*].rows`` shape (one entry per
                # sub-query, each with its own ``label`` / ``sql``). The
                # gate below now also fires for that tool, and the body
                # iterates per-sub-query so each lands in
                # ``_contract.answer_datasets()`` separately — this is what
                # enables ``_force_llm_synthesis`` to receive the runtime
                # agent's fetched rows at all (the empty-bubble guarantee's
                # _last_rows comes from goal_contract.answer_datasets()).
                if (
                    tool_name in DATA_PRODUCING_TOOLS
                    and isinstance(result, dict)
                    and settings.QUERY_PURPOSE_TAGGING_ENABLED
                    and (
                        result.get("rows")
                        or (
                            tool_name == "fetch_data_batch"
                            and result.get("results")
                        )
                    )
                ):
                    # Build the per-call dataset input(s). For top-level
                    # data tools (ask_data_agent / execute_query / …) there's
                    # exactly ONE entry. For fetch_data_batch the row array is
                    # nested PER sub-query, so we record one dataset per sub-
                    # query — they all carry their own sql + label.
                    _dataset_inputs: list[dict] = []
                    if tool_name == "fetch_data_batch":
                        for _sub in result.get("results") or []:
                            if not isinstance(_sub, dict):
                                continue
                            _sub_rows = _sub.get("rows") or []
                            if not _sub_rows:
                                continue
                            _dataset_inputs.append({
                                "rows": _sub_rows,
                                "sql": _sub.get("sql"),
                                "source_name": (
                                    str(_sub.get("label") or "") or None
                                ),
                                "source_id": _sub.get("source_id"),
                            })
                    elif result.get("rows"):
                        _dataset_inputs.append({
                            "rows": result.get("rows"),
                            "sql": result.get("sql"),
                            "source_name": result.get("source_name"),
                            "source_id": result.get("source_id"),
                        })

                    # Classify each dataset's purpose (probe/auxiliary/answer)
                    # and call ``record_dataset`` only on answer-tagged ones.
                    # Falls back to ``"answer"`` on classifier error so we
                    # never lose data to a transient failure.
                    for _ds in _dataset_inputs:
                        try:
                            _purpose = classify_query_purpose(
                                sql=_ds["sql"],
                                rows=_ds["rows"],
                                table_roles=(
                                    _purpose_resolver.roles_for(
                                        extract_tables_from_sql(
                                            _ds["sql"] or ""
                                        )
                                    )
                                    if _purpose_resolver is not None
                                    else {}
                                ),
                            )
                        except Exception as purpose_err:
                            logger.warning(
                                "v3 query-purpose classification failed "
                                "(fail-open answer) conv=%s: %s",
                                conversation_id, purpose_err,
                            )
                            _purpose = "answer"
                        if _purpose == "answer" and _contract is not None:
                            _contract.record_dataset(
                                rows=_ds["rows"],
                                sql=_ds["sql"],
                                source_name=_ds["source_name"],
                                source_id=_ds["source_id"],
                                purpose=_purpose,
                                tool_call_id=tool_call_id,
                            )
                        logger.info(
                            "v3 query purpose=%s rows=%d conv=%s "
                            "(tool=%s, card deferred to post-loop)",
                            _purpose, len(_ds["rows"]), conversation_id,
                            tool_name,
                        )
                    # Surface the dominant purpose on the top-level result so
                    # downstream callers that read ``result["query_purpose"]``
                    # still see something useful (last sub-query wins for
                    # fetch_data_batch; first/only wins otherwise). The gate
                    # above ensures ``_dataset_inputs`` is non-empty here, so
                    # ``_purpose`` is well-defined; the ``or "answer"`` is
                    # belt-and-suspenders against any classifier exception
                    # that left _purpose unset.
                    if _dataset_inputs:
                        result["query_purpose"] = _purpose or "answer"
                elif tool_name == "ask_data_agent" and isinstance(result, dict) and result.get("rows"):
                    # Legacy eager synthesis (flag OFF) — behavior unchanged.
                    try:
                        _synth_endpoint = effective_llm.endpoint

                        async def _synth_call(system_prompt, msgs, _ep=_synth_endpoint):
                            return await _call_synthesis_llm(
                                system_prompt, msgs, endpoint=_ep
                            )

                        # FIX 2026-08-24: merge rows from EVERY ask_data_agent
                        # call so far this turn, not just the current one.
                        # Previously rows=result.get("rows") meant multi-query
                        # turns (sales + revenue + inventory) synthesized from
                        # the LAST query only — the final answer ignored all
                        # earlier datasets.
                        # ALSO FIX: skip metadata-only datasets (SELECT COUNT(*)
                        # probes) so the synthesis only sees real business rows.
                        _merged_rows: list[dict] = []
                        _merged_seen: set[str] = set()
                        try:
                            from app.services.goal_contract import (
                                is_metadata_only_rows as _imor,
                            )
                        except Exception:
                            _imor = None
                        for _m_tc in tool_calls_for_frontend:
                            if _m_tc.get("name") != "ask_data_agent":
                                continue
                            _m_res = _m_tc.get("results") or {}
                            if not isinstance(_m_res, dict):
                                continue
                            _m_rows = _m_res.get("rows") or []
                            # Skip this dataset if it is a metadata-only probe
                            # (1-2 rows, every column a MIN/MAX/COUNT aggregate).
                            if (
                                _imor is not None
                                and _m_rows
                                and len(_m_rows) <= 2
                                and _imor(_m_rows)
                            ):
                                continue
                            for _m_row in _m_rows:
                                if not isinstance(_m_row, dict):
                                    continue
                                _m_key = json.dumps(
                                    _m_row, sort_keys=True, default=str,
                                    ensure_ascii=False,
                                )
                                if _m_key not in _merged_seen:
                                    _merged_seen.add(_m_key)
                                    _merged_rows.append(_m_row)
                        _synth_rows = _merged_rows or result.get("rows")

                        # FIX 2026-08-23: resolve skill context for synthesis
                        _v3e_skill_name, _v3e_skill_method = _resolve_skill_for_synthesis(
                            tool_calls_for_frontend,
                            selected_skill,
                            selected_skill_id,
                            db,
                        )
                        synth_result = await synthesize_report(
                            user_message=user_content,
                            rows=_synth_rows,
                            sql=result.get("sql"),
                            source_name=result.get("source_name"),
                            source_id=result.get("source_id"),
                            call_llm_fn=_synth_call,
                            skill_name=_v3e_skill_name,
                            skill_methodology=_v3e_skill_method,
                        )
                        if synth_result.report_card_payload is not None:
                            # Always attach payload + synthesis text so the
                            # frontend can suppress DataTableCard and show
                            # the narrative.
                            result["report_card_payload"] = synth_result.report_card_payload.model_dump()
                            result["synthesis_text"] = synth_result.assistant_content
                            # ── FINALIZE: persist Artifact row + HTML blob ──
                            # FIX 2026-08-22: only create an artifact card
                            # when the user explicitly asked for a file
                            # deliverable. For simple data questions the
                            # synthesis text IS the answer — no card needed.
                            if _orch_doc_format:
                                # NOTE: finalize_into_artifact may mutate
                                # `payload.user_signal` in-place when a
                                # file-format intent is detected (e.g. the
                                # user asked for DOCX).  We MUST re-serialize
                                # the payload AFTER finalize so the mutated
                                # field reaches the frontend.
                                # Offload finalize to a thread: the sandbox
                                # render can block 30-120s; if we await it
                                # directly the SSE heartbeat starves and the
                                # connection drops. Commit the caller's session
                                # first, then run with heartbeat coverage.
                                _finalize_task = _start_finalize_offloaded(
                                    db,
                                    {
                                        "conversation_id": conversation_id,
                                        "agent_name": agent_name,
                                        "user_message": user_content,
                                        "source": result.get("source_name"),
                                        "sql": result.get("sql"),
                                        "payload": synth_result.report_card_payload,
                                        "message_id": assistant_msg_id,
                                    },
                                )
                                async for _hb in _emit_tool_progress_while_waiting(
                                    _finalize_task,
                                    [
                                        {
                                            "tool_call_id": "finalize-artifact",
                                            "tool_name": "create_artifact",
                                            "args_str": "",
                                            "args": {},
                                        }
                                    ],
                                ):
                                    yield _hb
                                artifact, file_exports = _finalize_task.result()
                                # Re-serialize AFTER finalize so user_signal
                                # reflects any change (e.g. "export_docx").
                                result["report_card_payload"] = synth_result.report_card_payload.model_dump()

                                if file_exports:
                                    result["file_exports"] = file_exports
                                    # Prefer the file-export artifact_id so
                                    # the frontend picks up the DOCX/PPTX
                                    # preview instead of the HTML artifact.
                                    primary_fmt = next(iter(file_exports))
                                    export = file_exports[primary_fmt]
                                    export_artifact_id = export.get("artifact_id")
                                    if export_artifact_id:
                                        result["artifact_id"] = export_artifact_id
                                        artifact_ids.append(export_artifact_id)
                                if artifact is not None:
                                    # Keep the HTML artifact_id as a fallback
                                    # when no file export was generated.
                                    if not file_exports:
                                        result["artifact_id"] = artifact.id
                                        artifact_ids.append(artifact.id)
                            else:
                                artifact = None
                                file_exports = {}
                                logger.info(
                                    "FINALIZE: skipped artifact creation (no file intent) for conv=%s",
                                    conversation_id,
                                )
                    except Exception as synth_err:
                        logger.warning(
                            "Report synthesis failed (non-fatal) for conv=%s: %s",
                            conversation_id, synth_err,
                        )
                elif tool_name == "ask_data_agent" and _should_finalize_no_data(result, _orch_doc_format):
                    # ── Empty rows + file-format intent ──────────────────
                    # After the smart retry (above) exhausted its budget the
                    # query still returned 0 rows. Per the user's rule ("if
                    # there's no data, how would the agent make the file?"),
                    # we do NOT render an empty PPTX/DOCX — an empty artifact
                    # misleads and confuses. Instead we emit a clear no-data
                    # report card + narrative and mark the turn so the
                    # post-loop artifact fallback is skipped (an empty file
                    # generated from the generic fallback text is worse than
                    # a clean "no data" card).
                    try:
                        no_data_payload = build_no_data_payload(
                            user_message=user_content,
                            source=result.get("source_name"),
                            sql=result.get("sql"),
                        )
                        result["report_card_payload"] = no_data_payload.model_dump()
                        result["no_data"] = True
                        result["synthesis_text"] = (
                            no_data_payload.summary
                            or "No data was found for the requested filters/time period."
                        )
                        _orch_no_data[0] = True
                        logger.info(
                            "v3 NO-DATA (card only, no empty file): conv=%s (doc_format=%s)",
                            conversation_id, _orch_doc_format,
                        )
                    except Exception as no_data_err:
                        logger.warning(
                            "v3 No-data card failed (non-fatal) for conv=%s: %s",
                            conversation_id, no_data_err,
                        )

                # ── Auto-cache hook (flag-gated, fail-open) ────────────
                # Persist the FINAL enriched tool result (after smart-retry,
                # synthesis, and no-data card mutations above) so downstream
                # resume/intent-router flows can reuse cached data executions.
                # `_execution_id` is injected into the result dict and must
                # flow to the frontend record + message append that follow.
                if (
                    getattr(settings, "DATA_EXECUTION_CACHE_ENABLED", False)
                    and isinstance(result, dict)
                    and not result.get("requires_approval")
                    and result.get("success") is not False
                ):
                    try:
                        exec_id = await cache_data_execution(
                            db=db,
                            session_id=conversation_id,
                            tool_name=tool_name,
                            args=call.get("args") if isinstance(call, dict) else None,
                            result=result,
                            summary_text=None,
                            org_id=getattr(user, "org_id", None),
                            app_id=getattr(user, "app_id", None),
                        )
                        if exec_id:
                            result = {**result, "_execution_id": exec_id}
                    except Exception as exc:
                        logger.warning(
                            "data-execution cache failed (fail-open) conv=%s: %s",
                            conversation_id, exc,
                        )

                # 2026-08-28: never persist a guardrail-BLOCKED fusion360_clear
                # into the tool history. The model would read its synthetic
                # result ("CLEARED bodies: 0") as proof the scene is empty,
                # and a follow-up "update the model" turn then re-syncs in
                # circles instead of modifying the existing bodies.
                if (
                    isinstance(result, dict)
                    and isinstance(result.get("guardrail"), dict)
                    and result["guardrail"].get("code")
                    == "fusion_destructive_reclear_block"
                ):
                    logger.info(
                        "v3 stream: blocked clear omitted from tool history "
                        "(conv=%s, iter=%d)", conversation_id, iteration,
                    )
                    continue
                tool_call_record = {
                    "id": tool_call_id, "name": display_name,
                    "arguments_string": args_str, "results": result,
                    "status": "completed" if result.get("success") else "failed",
                }
                if tool_name in INTERNAL_TOOLS:
                    tool_call_record["display_projection"] = _internal_tool_projection(tool_name)
                tool_calls_for_frontend.append(tool_call_record)

                # ── Fix 2a: superseded-marker detection ──────────────
                # After each ask_data_agent result, mark earlier empty/error
                # results (same bound KB) as superseded so deck/card
                # generation never cites a stale/dead query. Also marks a
                # prior result superseded when the assistant text between
                # the two calls contains explicit retry hints.
                if tool_name == "ask_data_agent" and isinstance(result, dict):
                    inter_call_text = assistant_content[_ask_data_content_len[0]:]
                    _mark_superseded_ask_data_priors(
                        tool_calls_for_frontend, result, inter_call_text,
                    )
                    _ask_data_content_len[0] = len(assistant_content)

                # ── Goal-Contract runtime updates (flag-gated) ───────
                # Feed tool outcomes into the turn contract so the exit
                # checker can verify the goal was actually achieved (artifact
                # produced from non-empty data, query rows returned, announced
                # tool executed). Pure in-memory work; no new I/O.
                if _contract is not None and isinstance(result, dict):
                    # A query tool call that returned no usable rows (empty /
                    # all-null / metadata-only snapshot, or a failed call with
                    # no rows payload) must NOT count as fulfilling an
                    # announced action — pass the quality down so
                    # _announced_executed stays unset and the pending-action
                    # (now: _executed_seq is not stamped, so _armed_seq >
                    # _executed_seq on exit)
                    # remediation can still fire on exit.
                    _query_rows = result.get("rows")
                    _query_quality = (
                        RESULT_QUALITY_NO_DATA
                        if (
                            is_effective_empty(_query_rows)
                            or is_metadata_only_rows(_query_rows)
                        )
                        else RESULT_QUALITY_ASSUMED_OK
                    )
                    _contract.record_tool_executed(
                        tool_name, result_quality=_query_quality
                    )
                    # Query results: only a real rows payload counts (a bare
                    # sql key without rows must not register a zero-row event).
                    if isinstance(result.get("rows"), list):
                        _contract.record_query_result(
                            result.get("rows"), result.get("sql"),
                        )
                    # Artifacts / dashboards: any successful produce call
                    # satisfies the deliverable; kind is mapped to the contract
                    # canonical form so _deliverable_produced() can match.
                    _gc_produced = bool(
                        result.get("artifact_id")
                        or result.get("dashboard")
                        or result.get("dashboard_app")
                    )
                    if result.get("success") and _gc_produced:
                        if result.get("dashboard") or result.get("dashboard_app"):
                            _gc_kind = "dashboard"
                        else:
                            _gc_kind = result.get("kind") or result.get("type") or tool_name
                        _gc_rows = result.get("rows")
                        _contract.record_artifact(
                            kind=str(_gc_kind).lower(),
                            ok=True,
                            rows=(len(_gc_rows) if isinstance(_gc_rows, list) else None),
                        )

                # ── Dynamic budget upgrade ───────────────────────────
                # describe_schema revealed schema-graph join edges: widen
                # the per-turn soft cap so the agent can explore the joins
                # instead of being nudged to wrap up too early. Idempotent
                # (runs once, on the first edge-bearing result). T12: once
                # the describe_schema cap fires for a dashboard turn, the
                # upgrade is frozen so the soft cap stops widening.
                if (
                    not _schema_budget_frozen
                    and not _schema_edges_seen
                    and tool_name == "describe_schema"
                ):
                    _edge_count = _schema_edge_count(result)
                    if _edge_count:
                        _schema_edges_seen = True
                        _upgraded_budget = calculate_agent_budget(
                            [{"confidence": 0.9}] * min(_edge_count, 3),
                            user_content or "", is_automation=_auto_flag,
                        )
                        if _upgraded_budget > _effective_budget:
                            logger.info(
                                "v3 stream: schema graph revealed %d join edge(s); "
                                "per-turn budget %d->%d (conv=%s)",
                                _edge_count, _effective_budget,
                                _upgraded_budget, conversation_id,
                            )
                            _effective_budget = _upgraded_budget

                # Gap B checkpoint: the moment a report card or artifact
                # exists, persist it — a dropped SSE stream or crash later
                # in the turn must not erase finished work.
                if isinstance(result, dict) and (
                    result.get("report_card_payload") or result.get("artifact_id")
                ):
                    _checkpoint_partial_assistant_msg(
                        db, conv, messages, assistant_msg_id,
                        tool_calls_for_frontend, artifact_ids,
                    )

                # Live artifact event (Claude-style side panel): emit the
                # canonical artifact shape the moment it exists so the
                # frontend can open/update the preview pane mid-turn
                # instead of waiting for the `done` event. Additive SSE
                # type — old clients ignore it.
                if (
                    isinstance(result, dict)
                    and result.get("success")
                    and result.get("artifact_id")
                    and tool_name in ("create_artifact", "run_sandbox_skill")
                ):
                    _live_art = {
                        "artifact_id": result.get("artifact_id"),
                        "version_id": result.get("version_id"),
                        "version_number": result.get("version_number"),
                        "file_url": result.get("file_url"),
                        "preview_url": result.get("preview_url"),
                        "title": result.get("title", ""),
                        "type": result.get("type", ""),
                        "file_name": result.get("file_name", ""),
                        "mime_type": result.get("mime_type", ""),
                        "file_size": result.get("file_size"),
                        "has_preview": result.get("has_preview", False),
                    }
                    if result.get("preview_artifact_id"):
                        _live_art["preview_artifact_id"] = result["preview_artifact_id"]
                    yield f'data: {json.dumps({"type": "artifact_created", "artifact": _live_art})}\n\n'

                # ── Activity step: mark done/failed ──
                try:
                    step_num = _tool_step_indices[idx] if idx < len(_tool_step_indices) else None
                except NameError:
                    step_num = _step_counter[0]  # fallback for intercept path
                if step_num is not None:
                    new_status = "done" if result.get("success") else "failed"
                    # Reuse the description from the stored "running" step
                    # so that any (N/M) suffix is preserved on the done/failed
                    # update too.
                    step_desc = None
                    for s in _activity_steps:
                        if s.get("number") == step_num:
                            step_desc = s.get("description", "")
                            s["status"] = new_status
                            break
                    if not step_desc:
                        step_desc = _format_activity_description(tool_name, call.get("args"))
                    # Claude-style expandable detail: WHAT ran + WHAT came back.
                    _step_cmd = _summarize_tool_command(tool_name, call.get("args"))
                    _step_out = _summarize_tool_output(result)
                    _step_art = result.get("artifact_id") if isinstance(result, dict) else None
                    # Persist the detail fields on the stored step so the
                    # final message save keeps them (survives page refresh).
                    for s in _activity_steps:
                        if s.get("number") == step_num:
                            if _step_cmd:
                                s["command"] = _step_cmd
                            if _step_out:
                                s["output_preview"] = _step_out
                            if _step_art:
                                s["artifact_id"] = _step_art
                            break
                    _step_dur_ms = int((time.monotonic() - _step_start_times.get(step_num, time.monotonic())) * 1000)
                    yield _emit_activity_step(
                        step_num, step_desc, new_status, tool_name=tool_name,
                        command=_step_cmd, output_preview=_step_out,
                        artifact_id=_step_art,
                        duration_ms=_step_dur_ms,
                    )
                    # Live feed: typed tool-completion event with structured
                    # counts/duration (content invariant enforced upstream).
                    _le_rowcount = result.get("row_count") if isinstance(result, dict) else None
                    if not isinstance(_le_rowcount, int) or _le_rowcount < 0:
                        _le_rowcount = None
                    _le_params: dict = {
                        "tool_label": step_desc or tool_name,
                        "duration": round(_step_dur_ms / 1000, 1),
                    }
                    if _le_rowcount is not None:
                        _le_params["row_count"] = _le_rowcount
                    _le_fin = _push_live_event(
                        "tool_call_finished" if new_status == "done" else "tool_call_failed",
                        "tool_call_finished" if new_status == "done" else "tool_call_failed",
                        _le_params,
                    )
                    if _le_fin:
                        yield _le_fin
                    # Sub-agent return row — pairs with the invoked badge above
                    # so the user can see the delegation's outcome + duration.
                    if tn.startswith("ask_"):
                        _le_sub_ret_params: dict = {
                            "agent_label": step_desc or tool_name,
                            "duration": round(_step_dur_ms / 1000, 1),
                        }
                        if _le_rowcount is not None:
                            _le_sub_ret_params["row_count"] = _le_rowcount
                        _le_sub_ret = _push_live_event(
                            "subagent_returned", "subagent_returned",
                            _le_sub_ret_params,
                        )
                        if _le_sub_ret:
                            yield _le_sub_ret
                        # 2026-08-25: pop the open subagent stack — the
                        # delegation is complete. This matches the
                        # subagent_invoked that pushed earlier.
                        if _subagent_id_stack and _subagent_id_stack[-1]["target"] == tn:
                            _subagent_id_stack.pop()
                    # Inline data peek attached to the matching tool row.
                    # Only fires when the tool actually returned rows, so the
                    # preview pane never shows up on non-data tools.
                    if _le_rowcount and _le_rowcount > 0 and isinstance(result, dict):
                        _sample = _sample_rows_from_payload(result)
                        if _sample and _sample["columns"] and _sample["sample_rows"]:
                            _le_offer = _push_live_event(
                                "data_offer", "data_offer",
                                {
                                    "tool_label": step_desc or tool_name,
                                    "row_count": _le_rowcount,
                                    "columns": _sample["columns"],
                                    "sample_rows": _sample["sample_rows"],
                                },
                            )
                            if _le_offer:
                                yield _le_offer
                            # 2026-08-25: live-streaming spec — also emit a dedicated
                            # data_preview SSE event that bypasses the _LIVE_EVENT_CAP.
                            # This lets the UI show a live mini-grid under the tool row
                            # immediately when the query returns rows, not when the
                            # synthesis step completes.
                            try:
                                _tc_id = f"tc-{_step_counter[0]}"
                                yield f'data: {json.dumps({"type": "data_preview", "tool_call_id": _tc_id, "columns": _sample["columns"], "sample_rows": _sample["sample_rows"], "rows_so_far": _le_rowcount})}\n\n'
                            except Exception as _dp_err:
                                logger.debug("data_preview emit failed (non-fatal): %s", _dp_err)

                llm_messages.append({
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": tool_call_id, "type": "function",
                                    "function": {"name": tool_name, "arguments": args_str}}],
                })
                # P0: apply Layer 2 (per-result) persistence + guardrail after_call
                _result_str = _persisted_result_str(
                    tool_name, result, conversation_id,
                    context_window_tokens=(
                        effective_llm.endpoint.context_window
                        if effective_llm.endpoint else None
                    ),
                )
                # P1-5 Hermes-style hard cap: cap the individual tool result
                # to FSM_PRUNE_MIN_RESULT_CHARS (50k chars ≈ 12-15k tokens)
                # BEFORE appending to llm_messages.  This is Hermes Step 1
                # (hard tool output caps) — prevents any single result from
                # being too large, regardless of what the tool returned.
                from app.services.agent_loop.fsm_pruner import hard_cap_tool_result
                _result_str = hard_cap_tool_result(_result_str)
                llm_messages.append({
                    "role": "tool", "tool_call_id": tool_call_id,
                    "content": _result_str,
                })
                guard_ctrl.after_call(tool_name, call["args"], _result_str)
                # FIX 2026-08-24: harvest rich-text answers from market-research
                # sub-agents (ask_perception/ask_intelligence/ask_diagnosis/
                # ask_decision/ask_pricing). These return prompt_text instead of
                # rows; without this hook their text is silently discarded and
                # the post-loop join produces nothing, so the user gets the
                # generic "I gathered some information" fallback. Appending to
                # _v3_iter_contents here means the post-loop join includes it in
                # accumulated_content, the empty-bubble guarantee never fires,
                # and the final content_replace shows the real report.
                if tool_name in TEXT_PRODUCING_TOOLS:
                    _harv = _harvest_text_answer(result)
                    if _harv:
                        _v3_iter_contents.append(_harv)
                        logger.info(
                            "v3 captured text answer from %s (%d chars, conv=%s)",
                            tool_name, len(_harv), conversation_id,
                        )
                # P8: Record learning
                try:
                    from app.services.learning_graph import record_learning as _rl
                    _rl(agent_app_id, f"called {tool_name}",
                        "success" if isinstance(result, dict) and result.get("success") else "failure",
                        context=(user_content or "")[:200], tool=tool_name)
                except Exception:
                    pass

            # ── Clarify handoff: suspend the turn (2026-08-28) ─────────
            # A successful clarify means the user's next message IS the
            # answer. End the loop NOW — before the model can burn the
            # remaining budget on guard blocks or failing tools (the tool
            # budget cap of 3 and the "do NOT call this again" instruction
            # were insufficient: deepseek re-issued clarify 2x then tried
            # blocked tools until the iteration budget died with a confusing
            # verify_failed on the Sales Performance Dashboard turn).
            if _clarify_issued:
                if not assistant_content and _clarify_question_text:
                    assistant_content = _clarify_question_text
                    logger.info(
                        "v3 stream: clarify question used as final content "
                        "(conv=%s, iter=%d)",
                        conversation_id, iteration,
                    )
                logger.info(
                    "v3 stream: clarify issued (conv=%s, iter=%d); "
                    "suspending turn awaiting user answer",
                    conversation_id, iteration,
                )
                break

            # P0: Layer 3 — apply per-turn aggregate budget to this batch's results
            if not paused_for_approval:
                _batch_ids = [c["tool_call_id"] for c in parsed_calls]
                _batch_names = [c["tool_name"] for c in parsed_calls]
                _apply_turn_budget_to_messages(
                    llm_messages, _batch_ids, _batch_names, conversation_id,
                    context_window_tokens=(
                        effective_llm.endpoint.context_window
                        if effective_llm.endpoint else None
                    ),
                )

            # P0: if guardrail controller tripped a halt, inject nudge and break
            if not paused_for_approval and guard_ctrl.halt_decision:
                _hd = guard_ctrl.halt_decision
                logger.warning(
                    "Guardrail halt in conversation %s (stream): %s (tool=%s, count=%d)",
                    conversation_id, _hd.code, _hd.tool_name, _hd.count,
                )
                metrics.record_guardrail_halt(_hd.code)
                llm_messages.append({
                    "role": "user",
                    "content": (
                        f"A tool loop was detected: {_hd.message} "
                        "Use the results you already have and produce your final answer."
                    ),
                })
                break

            # P0: refund iteration for execute_code turns
            if not paused_for_approval and all(c["tool_name"] == "execute_code" for c in parsed_calls):
                if all(isinstance(r, dict) and r.get("success") is True for r in results):
                    conv_budget.refund()

            # ReAct reflexion: if any tool in this batch failed, inject a
            # critique system message so the next iteration reasons about
            # the failure instead of blindly retrying.  Guarded by
            # ``not paused_for_approval`` because an approval break leaves
            # un-appended results in ``results`` that must not be critiqued.
            if not paused_for_approval:
                _inject_reflexion_critique(llm_messages, parsed_calls, results)

            # Emit tool progress event
            yield f'data: {json.dumps({"type": "tool_progress", "tool_calls": tool_calls_for_frontend})}\n\n'

            # Force-pause (R6): if the LLM has been exploring for 2+
            # iterations without ever calling `create_agent`, and the user
            # message contains a save-directly / build-it cue, build a
            # decision summary from the user message + sensible defaults
            # and pause for the Decision Summary card. This breaks the
            # discovery loop deterministically — without it, DeepSeek
            # tends to narrate "Presenting the decision summary" in prose
            # and keep calling list_tools/skills forever, leaving the user
            # staring at "Searching available capabilities..." spinners.
            #
            # IMPORTANT: trigger on the accumulated tool-call COUNT, not
            # the iteration counter. LLMs that support parallel tool
            # calls (DeepSeek, GPT-4, Claude) issue ALL discovery calls
            # in a single iteration. The classic 3-call discovery
            # pattern (list_tools + search_skills + list_knowledge_bases)
            # happens at iteration 0, so iteration >= 2 never fires.
            # `len(tool_calls_for_frontend) >= 2` is iteration-agnostic:
            # it fires whether the calls were parallel (1 iteration) or
            # sequential (multiple iterations). See
            # tests/test_force_pause_parallel_tools.py for the regression
            # test.
            if (
                len(tool_calls_for_frontend) >= 2
                and not intercepted
                and _user_wants_save_directly(user_content)
            ):
                forced = _build_forced_decision_summary(user_content)
                forced_clean = _sanitize_decision_payload(forced)
                if forced_clean.get("name"):
                    logger.info(
                        "force-pause (v3 stream): conv=%s iteration=%d "
                        "user_cue='save directly' forced_payload_keys=%s",
                        conversation_id, iteration,
                        sorted(forced_clean.keys()),
                    )
                    # Record a synthetic create_agent entry for the chat
                    # bubble so the user sees what's about to happen.
                    synthetic_id = f"forced_{uuid.uuid4()}"
                    tool_calls_for_frontend.append({
                        "id": synthetic_id,
                        "name": TOOL_DISPLAY_NAMES.get("create_agent", "create_agent"),
                        "arguments_string": json.dumps(forced_clean),
                        "status": "awaiting_decision_summary",
                        "forced": True,
                    })
                    paused, stripped_for_ui, _note = _persist_decision_summary_pause(
                        db, conv, messages, assistant_msg_id,
                        tool_calls_for_frontend, assistant_content,
                        tool_call_payload=forced_clean,
                    )
                    if paused:
                        if stripped_for_ui:
                            yield f'data: {json.dumps({"type": "delta", "content": stripped_for_ui})}\n\n'
                        # Add a short note explaining what we did.
                        note = (
                            "\n\nI have enough to build the agent. I'm pausing "
                            "here so you can review before I save it."
                        )
                        yield f'data: {json.dumps({"type": "delta", "content": note})}\n\n'
                        try:
                            conv = db.query(AgentConversation).filter(
                                AgentConversation.id == conversation_id,
                            ).first() or conv
                        except Exception:
                            pass
                        yield f'data: {json.dumps({"type": "paused", "reason": "awaiting_decision_summary", "conversation": conv.to_dict()})}\n\n'
                        return
                    # Sanitiser rejected — fall through. Very rare.

            # Persist in-progress state to the DB so that a dropped SSE
            # connection doesn't lose the work that already happened.
            # The full resume_state is only stored when paused; here we save
            # a lightweight checkpoint that the UI can use to recover.
            # NOTE: We must NOT rebind `messages` here — it's a closure
            # variable shared with the outer scope. Build a new list and
            # mutate `messages` in place via slicing so the outer binding
            # stays valid.
            try:
                base = list(messages)
                # Drop a trailing empty assistant placeholder (if any) so we
                # don't accumulate one assistant message per iteration.
                # The cleanup drops the placeholder whenever the trailing
                # assistant message has empty content — regardless of whether
                # it carries tool_calls. The previous version also required
                # "no tool_calls", which never fired because partials always
                # carry ``tool_calls_for_frontend`` (report cards, artifact
                # ids). Result: each checkpoint appended a new partial
                # without dropping the old, and ``conv.messages`` grew with
                # duplicate assistant messages. (Bug: user asked one question,
                # refresh showed multiple identical answers.)
                if base and base[-1].get("role") == "assistant" and not base[-1].get("content"):
                    base = base[:-1]
                base.append({
                    "id": str(uuid.uuid4()), "role": "assistant", "content": "",
                    "created_date": datetime.now(timezone.utc).isoformat(),
                    "tool_calls": tool_calls_for_frontend,
                })
                conv.messages = base
                # IMPORTANT: re-assign metadata_ to a NEW dict so SQLAlchemy
                # detects the change. Mutating in place (e.g. `md["k"]=v`)
                # does NOT trigger change detection for JSON columns.
                new_meta = dict(conv.metadata_ or {})
                new_meta["_last_checkpoint_at"] = datetime.now(timezone.utc).isoformat()
                conv.metadata_ = new_meta
                conv.updated_date = datetime.now(timezone.utc)
                db.commit()
                # Replace the closure variable's contents in place.
                messages[:] = base
                logger.info("v3 stream checkpoint saved: conv=%s msgs=%d tool_calls=%d", conversation_id, len(base), len(tool_calls_for_frontend))
            except Exception as _ckpt_err:
                logger.warning("v3 stream checkpoint save failed: %s", _ckpt_err)
                db.rollback()

            if paused_for_approval:
                # The checkpoint block above already committed conv.messages
                # and conv.metadata_. The instance is now expunged from the
                # session, so re-query a fresh one to attach the resume_state
                # and awaiting_approval status on top.
                try:
                    db.refresh(conv)
                except Exception:
                    conv = db.query(AgentConversation).filter(
                        AgentConversation.id == conversation_id,
                    ).first()
                if not conv:
                    logger.error("Conversation %s vanished after checkpoint commit", conversation_id)
                    return
                # IMPORTANT: build a new dict for metadata_ so SQLAlchemy's
                # change detection picks up the new key. Mutating the existing
                # dict in place does NOT trigger an UPDATE for JSON columns.
                new_meta = dict(conv.metadata_ or {})
                new_meta["_resume_state"] = {
                    "llm_messages": llm_messages, "iteration": iteration,
                    "tool_calls_for_frontend": tool_calls_for_frontend,
                    "agent_name": agent_name, "agent_app_id": agent_app_id,
                    "data_ctx_extras": data_ctx_extras, "user_content": user_content,
                    "guardrail_retries": guardrail_retries,
                    "system_prompt": system_prompt, "tools": tools,
                    "pending_tool": pending_tool,
                }
                conv.metadata_ = new_meta
                conv.status = "awaiting_approval"
                conv.messages = messages
                conv.updated_date = datetime.now(timezone.utc)
                db.commit()
                yield f'data: {json.dumps({"type": "paused", "conversation": conv.to_dict()})}\n\n'
                _discard_steer(conversation_id)
                return

        # --- Stream the final response ---
        _loop_exit_monotonic = time.monotonic()
        # Turn-plan final step (2026-08-27): when the loop produced content,
        # the answer/respond step is complete — deterministic, model-free.
        if (
            _turn_plan is not None
            and _turn_plan.steps
            and assistant_content.strip()
        ):
            _plan_final_done = mark_final_step_completed(
                _turn_plan, _turn_plan_completed, bool(assistant_content.strip()),
            )
            if _plan_final_done:
                _turn_plan_completed |= _plan_final_done
                for _plan_idx in sorted(_plan_final_done):
                    _plan_st = _turn_plan.step_by_index(_plan_idx)
                    if _plan_st is not None:
                        yield plan_step_completed_frame(_plan_st)
                        # Persist the final completion(s) so the reloaded
                        # checklist shows the full plan ticked off (parity
                        # with the live stream).
                        _le_done = _push_live_event(
                            "plan_step_completed", "plan_step_completed",
                            {"step_index": _plan_st.step_index, "title": _plan_st.title_en},
                            sanitize=False,
                        )
                        if _le_done:
                            yield _le_done
        # Use the ``assistant_content`` already captured by the main loop's
        # last non-streaming ``_call_llm_with_tools`` call. DO NOT re-call
        # the LLM here: when the message history still contains assistant
        # ``tool_calls`` from prior iterations and we pass ``tools=None``,
        # DeepSeek (and any model whose native tool-calling format is
        # in-band) cannot return structured ``tool_calls``, so it falls
        # back to emitting its native DSML tokens as plain text content,
        # e.g. "<｜｜DSML｜｜tool_calls> <｜｜DSML｜｜invoke name="...">".
        # That raw token soup streams verbatim to the UI and renders
        # inside the assistant chat bubble — the "raw text" symptom in
        # the Agent Builder. The non-streaming ``add_message`` v2 path
        # already does this correctly (uses the content the main loop
        # produced with no second LLM call), so we mirror that here.
        # BUGFIX (Fix A): `_orch_created` must exist before the fallback
        # reference above can evaluate it. It is (re)assigned by the
        # orchestrator block below; this early init provides a safe empty
        # default when the orchestrator is skipped (fallback path, error
        # path, or the dashboard-orchestrator guard).
        _orch_created: list[dict] = []

        # BUGFIX: skip fallback if deltas were already streamed — otherwise
        # the user sees "detailed report" + "I gathered some information..."
        # as a confusing double-message.
        if not assistant_content and not locals().get("content_streamed"):
            if agent_name == "agent_builder":
                assistant_content = _EMPTY_CONTENT_FALLBACK
            else:
                # NEW 2026-08-25: institutional-grade research-analyst
                # forced-synthesis. When the directive is active AND we
                # have data AND the main loop emitted empty content (the
                # common 'LLM dropped into tool_calls with empty content'
                # failure on weak models), make ONE focused synthesis
                # call before the existing data-table fallback. If that
                # succeeds, the user gets the institutional-grade
                # analysis they were promised. If it fails or is not
                # applicable, fall through to the existing chain.
                try:
                    research_text = await _research_analyst_fallback(
                        user_content=user_content,
                        tool_calls_for_frontend=tool_calls_for_frontend,
                        agent_name=agent_name,
                        agent_app=agent_app,
                        endpoint=effective_llm.endpoint,
                    )
                except Exception as _ra_exc:  # noqa: BLE001
                    logger.warning("_research_analyst_fallback raised: %s", _ra_exc)
                    research_text = None
                if research_text:
                    assistant_content = research_text
                else:
                    assistant_content = _choose_fallback(
                        tool_calls_for_frontend, _orch_created, user_content=user_content,
                    )

        # Decision-summary pause (R4): if the assistant text contains a
        # `:::decision-summary` block, persist the pending payload to
        # `conv.metadata_` and emit a `paused` SSE event so the frontend
        # can render the DecisionSummaryCard. We still stream the
        # (block-stripped) prose as a `delta` first so the user sees
        # the agent's recommendation text, then pause for review.
        paused, stripped_for_ui, _note = _persist_decision_summary_pause(
            db, conv, messages, assistant_msg_id,
            tool_calls_for_frontend, assistant_content,
        )
        if paused:
            # Stream the user-visible (block-stripped) text first — but
            # only if we haven't already streamed the (unstripped) text
            # token-by-token above. When STREAM_TOKEN_DELTAS is on, the
            # raw text was already sent; the frontend will receive the
            # cleaned version via the final `done` event's persisted
            # assistant message. Sending it again would duplicate.
            if not content_streamed:
                yield f'data: {json.dumps({"type": "delta", "content": stripped_for_ui})}\n\n'
            # Refresh the conversation so the SSE payload reflects the
            # newly-written `awaiting_decision_summary` metadata.
            try:
                conv = db.query(AgentConversation).filter(
                    AgentConversation.id == conversation_id,
                ).first() or conv
            except Exception:
                pass
            yield f'data: {json.dumps({"type": "paused", "reason": "awaiting_decision_summary", "conversation": conv.to_dict()})}\n\n'
            return

        # Phase headline: composing the final response = "Crystallizing".
        if "finalize" not in _emitted_phases:
            _emitted_phases.add("finalize")
            yield _emit_phase("finalize")
            # Live feed: honest verdict — verify_passed only when no tool
            # call failed; otherwise verify_failed (never claim success
            # after a step like a sandbox/artifact build actually failed).
            _any_failed_tool = any(
                str(tc.get("status") or "").lower()
                in ("failed", "error", "denied")
                for tc in (locals().get("tool_calls_for_frontend") or [])
            ) and not _clarify_issued
            _le_vp = _push_live_event(
                "verify_failed" if _any_failed_tool else "verify_passed",
                "verify_failed" if _any_failed_tool else "verify_passed",
            )
            if _le_vp:
                yield _le_vp
            _le_fs = _push_live_event("finalize_started", "finalize_started")
            if _le_fs:
                yield _le_fs
            # Persist the finalize phase as a typed live event so the
            # reloaded headline shows the terminal state ("Crystallizing /
            # Wrapping everything up") — same as the live stream. Without it,
            # the last persisted phase_enter would be the plan phase and the
            # completed message would look mid-flight after reload.
            _le_fin_phase = _push_live_event("phase_enter", "phase_enter.finalize")
            if _le_fin_phase:
                yield _le_fin_phase

        # ── Activity step: "Generating response" done ──
        _step_counter[0] += 1
        if _loop_exit_monotonic is not None:
            _gen_dur = int((time.monotonic() - _loop_exit_monotonic) * 1000)
        else:
            _gen_dur = None
        yield _emit_activity_step(
            _step_counter[0], "Generating response", "done",
            duration_ms=_gen_dur if _gen_dur is not None else 0,
        )
        _activity_steps.append({
            "number": _step_counter[0],
            "description": "Generating response",
            "status": "done",
            "duration_ms": _gen_dur if _gen_dur is not None else 0,
        })

        # ── Marker contract: ◤MD_DOCX◤ / ◤HTML_DOCX◤ / ◤PPTX◤ ──
        # Strip markers from the streamed + persisted assistant content, and
        # route each marker through the generation orchestrator (which properly
        # *awaits* the async _create_artifact_tool — the previous synchronous
        # call produced a never-awaited coroutine, so markers never yielded
        # artifacts). Also runs the server-driven doc-request fallback (Q1) so
        # a confirmed file request always yields an artifact. Best-effort: a
        # marker/fallback failure must never break the SSE stream.
        # (_orch_doc_format was computed before the tool loop.)
        # ── Fix C: dashboard-orchestrator guard ────────────────────────────
        # On dashboard-intent turns where the build tool was never called,
        # the post-loop orchestrator would auto-create a static HTML
        # "Dashboard" artifact (ensure_artifact_for_doc_request with
        # doc_format="dashboard"), bypassing DASHBOARD_ANTITOOLS — the exact
        # routing conflict observed on caeeda3b. Skip the whole orchestrator
        # block and surface a synthetic BLOCKED record so the UI shows why
        # no artifact appeared.
        _orch_guard_should_block = dashboard_orchestrator_should_block(
            user_content, _v3_executed_tool_names, _v3_failed_tool_names,
        )
        if _orch_guard_should_block:
            logger.warning(
                "v3 stream: dashboard-orchestrator guard blocked post-loop "
                "artifact creation on dashboard turn (conv=%s); build tool "
                "never called — skipping orchestrator.",
                conversation_id,
            )
            tool_calls_for_frontend.append({
                "id": f"orchguard_{uuid.uuid4()}",
                "name": "dashboard_orchestrator_guard",
                "status": "blocked",
                "results": {
                    "blocked": "orchestrator",
                    "reason": (
                        "post-loop orchestrator skipped on dashboard turn; "
                        "the dashboard should be built with "
                        "create_fullstack_dashboard"
                    ),
                },
            })
        try:
            if _orch_guard_should_block:
                raise _OrchGuardSkipped()
            from app.services.generation_orchestrator import (
                ensure_artifact_for_doc_request,
                fulfill_markers,
            )

            assistant_content, _orch_created = await fulfill_markers(
                assistant_content,
                db=db,
                context={
                    "conversation_id": conversation_id,
                    "agent_app_id": agent_app_id,
                },
            )
            # Also post-process the LLM's own [[RESULT]]...[[END]] blocks.
            # Some system prompts (and most LLM hallucinations) tell the
            # model to emit a [[RESULT]] block with a *fake* artifact id;
            # without this step the frontend renders a card pointing at a
            # non-existent artifact and the user sees a 404.  Fulfilling
            # here creates a real artifact and rewrites the id in the
            # assistant text so the resource card works end-to-end.
            try:
                from app.services.result_block_processor import (
                    fulfill_result_blocks,
                )

                assistant_content, _result_created = await fulfill_result_blocks(
                    assistant_content,
                    db=db,
                    context={
                        "conversation_id": conversation_id,
                        "agent_app_id": agent_app_id,
                    },
                )
                if _result_created:
                    _orch_created = list(_orch_created) + _result_created
            except Exception as _rb_err:
                logger.warning(
                    "v3 stream: result_block post-processor raised (non-fatal): %s",
                    _rb_err,
                )

            # ── Self-healing refusal guardrail ──────────────────────────
            # When the LLM says "I cannot browse" for an online-research
            # request, auto-run ``web_search`` and append the results to
            # ``assistant_content`` so the user always gets a real answer.
            # In the v3 stream path we DO NOT re-ask the LLM (would
            # require a second stream) — we just append the search
            # results so the user sees the corrected answer.
            try:
                from app.services.turn_action import check_and_fallback

                _rg_decision = await check_and_fallback(
                    user_message=user_content,
                    assistant_text=assistant_content,
                    db=db,
                )
                if _rg_decision.get("search_results"):
                    bullets = "\n".join(
                        f"- [{r.get('title','?')}]({r.get('url','?')}): {r.get('snippet','')}"
                        for r in _rg_decision["search_results"]
                    )
                    assistant_content = (
                        f"{assistant_content}\n\n"
                        f"---\n"
                        f"_I can browse the web — here is what I found for "
                        f"\"{_rg_decision['search_query']}\":_\n\n"
                        f"{bullets}"
                    )
            except Exception as _rg_err:
                logger.warning(
                    "v3 stream: refusal guardrail raised (non-fatal): %s",
                    _rg_err,
                )
            if not _orch_no_data[0]:
                _fallback = await ensure_artifact_for_doc_request(
                    doc_format=_orch_doc_format,
                    assistant_content=assistant_content,
                    already_created=_orch_created,
                    tool_calls_for_frontend=tool_calls_for_frontend,
                    artifact_ids=artifact_ids,
                    db=db,
                    context={
                        "conversation_id": conversation_id,
                        "agent_app_id": agent_app_id,
                        "user_message": user_content,
                    },
                )
                if _fallback:
                    _orch_created = list(_orch_created) + [_fallback]
        except _OrchGuardSkipped:
            pass  # Fix C: dashboard-orchestrator guard fired — orchestrator skipped
        except Exception as _marker_outer_err:
            logger.warning(
                "v3 Marker parsing block failed (non-fatal): %s",
                _marker_outer_err,
            )

        # Emit the final assistant content as a delta — but ONLY if it
        # was not already streamed token-by-token above (STREAM_TOKEN_DELTAS
        # path). Re-emitting would duplicate the text in the UI. The
        # legacy buffered path (flag off) and fallback paths
        # (guardrail/loop-guard/error-recovery) still need this emit
        # because they set assistant_content without streaming.
        if not content_streamed and not _suppress_chat_deltas:
            yield f'data: {json.dumps({"type": "delta", "content": assistant_content})}\n\n'

        # Surface orchestrator-created artifacts (marker fulfillment + the
        # server-driven doc fallback) as synthetic create_artifact tool_call
        # records so _collect_artifact_results links + exposes them exactly
        # like the LLM-driven path.
        # Bug 2 fix: post-loop fallback for an empty assistant_content.
        # When the tool-call loop guard trips (see _detect_tool_call_loop
        # above) we break without producing a final answer — the previous
        # implementation leaked the internal reflexion text as the
        # user's response. Now we fall back to a clean, agent-aware
        # message only if the LLM didn't already produce a response
        # across the tool iterations.
        # FIX 2026-08-22: compute whether the deferred synthesis will run
        # AFTER the loop, so we can skip the post-loop fallback placeholder
        # when synthesis is coming.  The conditions mirror the full check
        # at line ~12176.  Moving this earlier is safe because the tool loop
        # has already completed and the contract's answer_datasets are final.
        #
        # FIX 2026-08-24: gate the deferred synthesis on the PRESENCE of
        # answer data, not on collection_complete(). A trailing coverage
        # probe or one empty branch used to veto the entire synthesis stage
        # even when 70+ rows of business data had been collected — the turn
        # then fell through to the generic fallback despite having data.
        _deferred_answer = (
            _contract.answer_datasets()[-1]
            if (
                settings.DELIVERABLE_PHASE_LOCK_ENABLED
                and _contract is not None
                and _contract.has_answer_data()
                and not _orch_no_data[0]
                and not (
                    _create_artifact_titles
                    or "create_artifact" in _v3_executed_tool_names
                    or "run_sandbox_skill" in _v3_executed_tool_names
                )
                and _contract.answer_datasets()
            )
            else None
        )
        # Skip the post-loop fallback when synthesis will run. The synthesis
        # LLM is the authoritative source of the bubble text — falling back
        # first and then having to overwrite later is brittle and (before
        # the fix at ~12282) the "only replace if empty" condition meant
        # the placeholder stuck, so the report card appeared to "vanish".
        if not (assistant_content or "").strip() and _deferred_answer is None:
            try:
                # Dashboard-specific rescue: if the user asked for a
                # dashboard but the active build tool was never called,
                # inject a final nudge forcing the LLM to call it with
                # whatever schema info it has. The tool name is flag-aware
                # (dashboard_build_tool) so we never force the disabled
                # legacy create_dashboard. This prevents the common
                # "budget exhausted during exploration" failure.
                _rescue_build_tool = dashboard_build_tool()
                if (
                    _rescue_build_tool
                    and _is_dashboard_request(user_content)
                    and not _tool_was_called(tool_calls_for_frontend, _rescue_build_tool)
                    and not _tool_was_called(tool_calls_for_frontend, "update_fullstack_dashboard")
                    and not _tool_was_called(tool_calls_for_frontend, "update_dashboard")
                ):
                    logger.info(
                        "Dashboard rescue: user asked for dashboard but "
                        "%s never called (conv=%s). Injecting rescue nudge.",
                        _rescue_build_tool, conversation_id,
                    )
                    rescue_nudge = (
                        "CRITICAL: The user asked for a dashboard but you ran "
                        "out of tool iterations before calling "
                        + _rescue_build_tool + ". "
                        "You MUST call " + _rescue_build_tool + " NOW with the schema "
                        "information you already have. Use the column names you "
                        "discovered from describe_schema/execute_query. Do NOT "
                        "explore further — build the dashboard with what you know."
                    )
                    llm_messages.append({"role": "user", "content": rescue_nudge})
                    # One final LLM call to force the active build tool
                    try:
                        _rescue_stream = _stream_llm_with_tools(
                            llm_messages, tools,
                            tool_choice={"type": "function", "function": {"name": _rescue_build_tool}},
                            model_override=user_model,
                            endpoint=effective_llm.endpoint,
                            temperature=llm_overrides.get("temperature"),
                            max_tokens=llm_overrides.get("max_tokens"),
                        )
                        async for _ev_type, _ev_data in _rescue_stream:
                            if _ev_type == "delta":
                                assistant_content += _ev_data
                                content_streamed = True
                                if not _suppress_chat_deltas:
                                    yield f'data: {json.dumps({"type": "delta", "content": _ev_data})}\n\n'
                            elif _ev_type == "tool_calls":
                                raw_tool_calls = _ev_data or []
                        # If the rescue produced a build-tool call, execute it
                        if raw_tool_calls:
                            for _tc in raw_tool_calls:
                                _fn = _tc.get("function", {})
                                _tname = _fn.get("name", "")
                                if _tname == _rescue_build_tool:
                                    _targs = _fn.get("arguments", "{}")
                                    if isinstance(_targs, str):
                                        import json as _json
                                        try:
                                            _targs = _json.loads(_targs)
                                        except Exception:
                                            _targs = {}
                                    _tool_result = await execute_tool(
                                        _tname, _targs, db,
                                        user_id,
                                        context={
                                            "conversation_id": conversation_id,
                                            "agent_app_id": agent_app_id,
                                            "agent_name": agent_name,
                                            "conversation_metadata": conv.metadata_ or {},
                                            "chat_session_id": chat_session_id,
                                            **(data_ctx_extras or {}),
                                        },
                                    )
                                    tc_id = _tc.get("id", str(uuid.uuid4()))
                                    tool_calls_for_frontend.append({
                                        "id": tc_id,
                                        "name": _rescue_build_tool,
                                        "arguments_string": _fn.get("arguments", "{}") if isinstance(_fn.get("arguments"), str) else json.dumps(_fn.get("arguments", {})),
                                        "status": "completed" if isinstance(_tool_result, dict) and _tool_result.get("success") else "failed",
                                        "results": _tool_result,
                                    })
                                    if isinstance(_tool_result, dict):
                                        _dash_id = _tool_result.get("dashboard_id") or _tool_result.get("artifact_id")
                                        if _dash_id and _dash_id not in artifact_ids:
                                            artifact_ids.append(_dash_id)
                                    # Append tool result to messages for persistence
                                    llm_messages.append({"role": "assistant", "content": None, "tool_calls": [_tc]})
                                    llm_messages.append({"role": "tool", "tool_call_id": tc_id, "content": json.dumps(_tool_result)})
                                    logger.info(
                                        "Dashboard rescue: %s executed "
                                        "(success=%s, conv=%s)",
                                        _rescue_build_tool,
                                        isinstance(_tool_result, dict) and _tool_result.get("success"),
                                        conversation_id,
                                    )
                    except Exception as _rescue_err:
                        logger.warning(
                            "Dashboard rescue LLM call failed: %s (conv=%s)",
                            _rescue_err, conversation_id,
                        )
                # After rescue attempt (or if no rescue needed), check
                # if we still have empty content.
                if not (assistant_content or "").strip():
                    # BUGFIX: If the streaming loop already emitted deltas
                    # to the user (content_streamed=True), the user has
                    # already seen the answer. Do NOT append a second
                    # fallback message on top — it creates a confusing
                    # "detailed report + I gathered some information but
                    # had trouble..." double-message. Just leave
                    # assistant_content empty; the streamed deltas are the
                    # authoritative answer.
                    if locals().get("content_streamed"):
                        logger.info(
                            "Post-loop: content was already streamed; "
                            "skipping fallback append (conv=%s)",
                            conversation_id,
                        )
                    elif locals().get("_loop_guard_user_facing"):
                        assistant_content = _loop_guard_user_facing
                    else:
                        # Dashboard-specific fallback message (flag-aware tool
                        # name so the check matches what the agent actually had
                        # available this turn).
                        if _is_dashboard_request(user_content) and not _tool_was_called(
                            tool_calls_for_frontend,
                            locals().get("_rescue_build_tool") or "create_dashboard",
                        ):
                            assistant_content = (
                                "I ran out of steps while exploring the data for your dashboard. "
                                "Please try again — I'll create the dashboard more quickly this time."
                            )
                        else:
                            # Context-aware fallback: choose the best message
                            # based on what was produced this turn (report cards,
                            # artifacts, dashboard requests, or generic).
                            #
                            # 2026-08-26: LLM-FIRST fallback. The loop reached
                            # this point with empty assistant_content (tool-only
                            # turn, or streamed narration stripped by hygiene
                            # rules) — but the collected rows ARE the answer.
                            # Before falling to the deterministic
                            # `_choose_fallback` template (the rigid
                            # "Scope / Executive Summary / Top Performers"
                            # builder), make ONE focused LLM synthesis call so
                            # the user gets a professional, narrative answer
                            # (Kimi/Claude-style) written from the actual data.
                            # The template remains only as the absolute last
                            # resort when the LLM cannot write prose.
                            _fb_rows: list[dict] = []
                            for _tc in (tool_calls_for_frontend or []):
                                try:
                                    _fb_ex = _extract_data_rows_from_tool_call(_tc)
                                except Exception:
                                    _fb_ex = None
                                if _fb_ex:
                                    _fb_rows.extend(_fb_ex[0])

                            async def _fb_synth_call(system_prompt, msgs, _ep=effective_llm.endpoint):
                                return await _call_synthesis_llm(system_prompt, msgs, endpoint=_ep)

                            _fb_text = ""
                            if _fb_rows:
                                try:
                                    _fb_text = await _force_llm_synthesis(
                                        user_content,
                                        _fb_rows,
                                        synth_call_fn=_fb_synth_call,
                                        conversation_history=llm_messages,
                                        max_retries=1,
                                        endpoint=effective_llm.endpoint,
                                    )
                                except Exception as _fb_synth_err:
                                    logger.warning(
                                        "Post-loop LLM-first synthesis failed (conv=%s): %s",
                                        conversation_id, _fb_synth_err,
                                    )
                                    _fb_text = ""
                            if _fb_text and len(_fb_text.strip()) > 50:
                                assistant_content = _fb_text
                                logger.info(
                                    "Post-loop: LLM-first synthesis produced %d chars "
                                    "(rows=%d, conv=%s)",
                                    len(_fb_text), len(_fb_rows), conversation_id,
                                )
                            else:
                                assistant_content = _choose_fallback(
                                    tool_calls_for_frontend, _orch_created,
                                    user_content=user_content,
                                )
                logger.info(
                    "Post-loop fallback: empty assistant_content, using fallback message "
                    "(conv=%s, len=%d)",
                    conversation_id, len(assistant_content),
                )
            except Exception as _fallback_err:
                logger.warning("Post-loop fallback failed: %s", _fallback_err)
                assistant_content = _GENERIC_EMPTY_CONTENT_FALLBACK
        if _orch_created:
            for _art in _orch_created:
                tool_calls_for_frontend.append({
                    "id": f"orch-{_art.get('artifact_id')}",
                    "name": "create_artifact",
                    "status": "completed",
                    "results": _art,
                })
                if _art.get("artifact_id") and _art["artifact_id"] not in artifact_ids:
                    artifact_ids.append(_art["artifact_id"])

        # Multi-iter content accumulation (2026-08-20):
        # Capture the FINAL iteration's `assistant_content` (the per-iter
        # reset at the top of the loop only captures iter 1..N-1; iter N's
        # content survives in `assistant_content` after the loop). After
        # this append, `_v3_iter_contents` holds every iteration's prose
        # in order. We join with `\n\n` (markdown paragraph break) and
        # filter empty entries (tool-only iters with no prose) so the
        # result reads naturally as a single flowing response.
        if assistant_content:
            _v3_iter_contents.append(assistant_content)
        accumulated_content = "\n\n".join(c for c in _v3_iter_contents if c).strip()
        # Edge case: if no iter produced any prose (all-tool turn), fall
        # back to whatever assistant_content holds (fallback text or "").
        if not accumulated_content and assistant_content:
            accumulated_content = assistant_content

        # BUGFIX 2026-08-23: recover best prior iteration if nudges
        # discarded all good content. When a nudge fires, it pops the
        # last iteration from the accumulator. If the loop then exits
        # (max iterations / budget cap) before producing a real answer,
        # accumulated_content becomes empty or just a promise phrase.
        # We saved the longest popped entry in `_v3_recovered_best`.
        if _v3_recovered_best[0]:
            _cur_len = len(accumulated_content or "")
            _rec_len = len(_v3_recovered_best[0])
            if _rec_len > _cur_len and _rec_len > 200:
                logger.info(
                    "v3 post-loop: recovered best prior iter (%d chars) "
                    "over current (%d chars, conv=%s)",
                    _rec_len, _cur_len, conversation_id,
                )
                accumulated_content = _v3_recovered_best[0]

        # Post-loop promise-text cleanup (Fix 5): if the accumulated
        # content ends with a pending-action sentence ("Let me verify…"),
        # strip it. This handles the case where the model produced promise
        # text alongside tool calls (not in the exit branch) and the loop
        # broke at the budget cap without the exit checker ever seeing it.
        if accumulated_content and settings.GOAL_CONTRACT_ENABLED:
            _post_pending = pending_action_phrase(accumulated_content)
            if _post_pending:
                accumulated_content = _strip_trailing_pending(
                    accumulated_content, _post_pending,
                )
                if accumulated_content.strip():
                    accumulated_content = (
                        accumulated_content + "\n\nNote: this turn ended before completing the "
                        "additional verification described above. The current answer is based "
                        "on the data already returned."
                    ).strip()

        # Post-loop internal-reference hygiene (Bug 3 fix): drop trailing
        # sentences that reference loop iterations the user never saw
        # ("the discrepancy", "you're right", "as I mentioned earlier",
        # "let me re-query…"). Deterministic, flag-gated.
        if accumulated_content and settings.DELIVERABLE_PHASE_LOCK_ENABLED:
            _hygiene_stripped = _strip_internal_references(accumulated_content)
            if _hygiene_stripped != accumulated_content:
                logger.info(
                    "v3 internal-reference hygiene: stripped trailing residue "
                    "(conv=%s, %d chars -> %d chars)",
                    conversation_id, len(accumulated_content), len(_hygiene_stripped),
                )
                accumulated_content = _hygiene_stripped
            # SQL/plan-narration strip (defense-in-depth for models that
            # narrate SQL in the final iteration without emitting tool
            # calls): ```sql fences, bare SELECT paragraphs, and empty
            # "JSON Report Card" sections ("SQL leak", 2026-08-21).
            _sql_stripped = _strip_sql_narration(accumulated_content)
            if _sql_stripped != accumulated_content:
                logger.info(
                    "v3 sql-narration hygiene: stripped sql/json artifacts "
                    "(conv=%s, %d chars -> %d chars)",
                    conversation_id, len(accumulated_content), len(_sql_stripped),
                )
                accumulated_content = _sql_stripped

        # ── Deferred deliverable: single post-loop synthesis + finalize ──
        # Bug 1 fix: cards are built ONCE at the end of the turn, from the
        # FINAL answer-tagged dataset (later queries refine earlier ones).
        # Skipped when (a) flags off, (b) no contract / collection
        # incomplete, (c) no answer dataset, (d) an artifact was already
        # built this turn, or (e) the no-data card already fired.
        # _deferred_answer was already computed above (line ~11947) so we can
        # skip re-computing it here.  The early computation was added as part
        # of the FIX 2026-08-22 to let the post-loop fallback guard work.

        # FIX 2026-08-24: hoisted synthesis callable out of the deferred
        # branch so the empty-bubble guarantee can always invoke the LLM
        # when answer data exists, even when the deferred block was skipped.
        _synth_endpoint = effective_llm.endpoint

        async def _deferred_synth_call(system_prompt, msgs, _ep=_synth_endpoint):
            return await _call_synthesis_llm(system_prompt, msgs, endpoint=_ep)

        if _deferred_answer is not None:
            # FIX A+C (2026-08-22): synthesize from the FULL answer-tagged
            # dataset set, not just the last query.  The final query is often
            # a degenerate refinement (e.g. a 1-row data-quality check) —
            # the earlier totals/breakdown datasets must still feed the
            # report.  Rows are merged deterministically (record order kept,
            # identical rows deduplicated) and the payload carries a
            # per-dataset overview so the LLM sees the whole query picture.
            _all_answer_datasets = _contract.answer_datasets()
            _last_rows = merge_answer_rows(_all_answer_datasets)

            # ── Historical data fallback (follow-up file-format turns) ───
            # When the user asks for a file format change (e.g. "give me in
            # docx formate") in a follow-up turn, the agent re-queries the
            # database. If that re-query returns 0 rows, we fall back to data
            # that was successfully fetched in an earlier turn instead of
            # producing a useless "no data" file.
            _hist_rows: list[dict] = []
            _hist_sql = ""
            _hist_source = ""
            if is_effective_empty(_last_rows):
                from app.services.generation_orchestrator import (
                    _mine_historical_answer_rows,
                )
                _hist_rows = _mine_historical_answer_rows(messages)
                if _hist_rows:
                    logger.info(
                        "v3 deferred deliverable: using %d historical rows "
                        "from previous turn (conv=%s)",
                        len(_hist_rows), conversation_id,
                    )
                    # Also try to extract SQL / source from the historical
                    # tool result so the methodology section is accurate.
                    for _m in reversed(messages):
                        if _m.get("role") != "tool":
                            continue
                        _c = _m.get("content")
                        if not isinstance(_c, str):
                            continue
                        try:
                            _p = json.loads(_c)
                        except Exception:
                            continue
                        if not isinstance(_p, dict):
                            continue
                        _r = _p.get("result") or _p
                        if not isinstance(_r, dict):
                            continue
                        _hr = _r.get("rows")
                        if isinstance(_hr, list) and _hr:
                            _hist_sql = _r.get("sql") or ""
                            _hist_source = _r.get("source_name") or ""
                            break
                    _last_rows = _hist_rows

            if is_effective_empty(_last_rows):
                logger.info(
                    "v3 deferred deliverable skipped: all answer datasets empty "
                    "and no historical data (conv=%s)",
                    conversation_id,
                )
            else:
                # Data-quality gate: don't build an artifact from garbage data
                # (e.g. rows that contain only internal IDs like FENTRYID, or
                # rows that are MIN/MAX/COUNT aggregates like {row_count: 14275}).
                from app.services.generation_orchestrator import (
                    _validate_artifact_data_quality,
                )
                # NOTE: is_metadata_only_rows comes from the module-level
                # import (line ~193). Do NOT re-import it locally — a local
                # import shadows the global for the whole function scope and
                # causes UnboundLocalError at earlier unconditional uses.
                # FIX 2026-08-24: also reject MIN/MAX/COUNT-only result sets.
                # `_validate_artifact_data_quality` accepts "count" as a
                # business fragment (row_count contains "count"), so the shape
                # check alone misses the common pattern of the data agent
                # running SELECT COUNT(*) and treating the 1-row result as
                # real business data.
                _is_meta_only = (
                    bool(_last_rows)
                    and len(_last_rows) <= 2
                    and is_metadata_only_rows(_last_rows)
                )
                _dq = _validate_artifact_data_quality(_last_rows, user_content)
                if _is_meta_only:
                    _dq = {
                        "valid": False,
                        "reason": (
                            "the answer dataset contains only aggregate metadata "
                            "(MIN/MAX/COUNT) instead of business rows — the data "
                            "agent likely ran a row-count probe, not a real data "
                            "query"
                        ),
                    }
                if not _dq["valid"]:
                    logger.warning(
                        "v3 deferred deliverable skipped: data quality check failed "
                        "(%s) (conv=%s)",
                        _dq["reason"],
                        conversation_id,
                    )
                    _no_data_msg = (
                        f"I checked your warehouse for the data needed for your "
                        f"request, but {_dq['reason']}. If you expected records "
                        f"for this period, please verify the date range, table "
                        f"names, or filters."
                    )
                    _acc_text = (accumulated_content or "").strip()
                    _is_placeholder_now = (
                        not _acc_text
                        or _acc_text == _GENERIC_EMPTY_CONTENT_FALLBACK.strip()
                        or _acc_text.startswith("(The requested")
                        or _acc_text.startswith("Data retrieved (")
                        or _acc_text.startswith("Analyzing ")
                        or _acc_text.startswith("I gathered some information")
                        or _acc_text.startswith("Based on the analysis of")
                        or _acc_text.startswith("I've completed your request")
                        or _acc_text.startswith("Your deliverable")
                        or _acc_text.startswith("Here is the artifact")
                        # 2026-08-26: same strip-residue rule — a mangled
                        # fragment must not block the honest no-data message.
                        or (
                            0 < len(_acc_text) < _WEAK_CONTENT_MAX_CHARS
                            and len(_no_data_msg) > len(_acc_text) * 2
                        )
                    )
                    if _is_placeholder_now:
                        accumulated_content = _no_data_msg
                        try:
                            yield (
                                'data: '
                                + json.dumps({
                                    "type": "content_replace",
                                    "content": accumulated_content,
                                })
                                + '\n\n'
                            )
                        except Exception as _bubble_yield_err:
                            logger.debug(
                                "v3 deferred no-data bubble yield failed: %s",
                                _bubble_yield_err,
                            )
                else:
                    try:
                        _synth_endpoint = effective_llm.endpoint

                        async def _deferred_synth_call(system_prompt, msgs, _ep=_synth_endpoint):
                            return await _call_synthesis_llm(system_prompt, msgs, endpoint=_ep)

                        # FIX 2026-08-23: resolve skill context for deferred synthesis
                        _def_skill_name, _def_skill_method = _resolve_skill_for_synthesis(
                            tool_calls_for_frontend,
                            selected_skill,
                            selected_skill_id,
                            db,
                        )
                        # Prefer historical SQL/source when we fell back to
                        # historical rows so the docx methodology is accurate.
                        _use_sql = _deferred_answer.get("sql") or _hist_sql
                        _use_source = _deferred_answer.get("source_name") or _hist_source
                        _use_source_id = _deferred_answer.get("source_id")
                        _deferred_synth = await synthesize_report(
                            user_message=user_content,
                            rows=_last_rows,
                            sql=_use_sql,
                            source_name=_use_source,
                            source_id=_use_source_id,
                            call_llm_fn=_deferred_synth_call,
                            datasets=_all_answer_datasets if not _hist_rows else [],
                            skill_name=_def_skill_name,
                            skill_methodology=_def_skill_method,
                        )
                        # ── Synthesis prose guarantee (v2 — robust) ────────────
                        # The synthesis LLM sometimes returns ONLY a JSON block
                        # with no prose → _strip_json_block leaves empty prose →
                        # assistant_content falls back to payload.summary which
                        # is short → chat bubble shows generic fallback.
                        # Fix: 3-tier guarantee:
                        #   1. If prose < 150 chars → try forced LLM synthesis
                        #   2. If forced synthesis also fails → build answer from data
                        #   3. If no data rows → leave as-is (truly empty case)
                        _synth_prose = (_deferred_synth.assistant_content or "").strip()

                        # --- Tier 1: Forced LLM synthesis (when prose too short) ---
                        if len(_synth_prose) < 150 and _last_rows:
                            logger.info(
                                "v3 synthesis prose too short (%d chars), forcing "
                                "second synthesis (conv=%s)",
                                len(_synth_prose), conversation_id,
                            )
                            try:
                                # 2026-08-26: rewritten to use ALL rows
                                # (or first 200) instead of just 5, demand
                                # 400-800 words, and require the 5-section
                                # structure. The previous prompt was capped
                                # at 5-8 sentences which produced the
                                # "Analyzing N rows…" placeholder problem.
                                _rows_payload = _last_rows[:200] if len(_last_rows) > 200 else _last_rows
                                _rows_json = json.dumps(_rows_payload, default=str, ensure_ascii=False)
                                # Truncate to ~40KB to stay within token limits
                                if len(_rows_json) > 40_000:
                                    _rows_json = _rows_json[:40_000] + "\n... (truncated for length)"
                                # Pre-aggregated stats from ALL rows
                                _preagg = ""
                                try:
                                    from app.services.synexia.pre_aggregation import pre_aggregate
                                    _preagg = pre_aggregate(_last_rows).to_prompt_block()
                                except Exception:
                                    _preagg = ""
                                _forced_prompt = (
                                    f"You are a senior data analyst. The user asked: \"{user_content[:300]}\"\n\n"
                                    f"You queried the database and got **{len(_last_rows)} rows** of real business data.\n\n"
                                    "Write a COMPREHENSIVE 400-800 word business report based on what you see. Use the user's question as your framing.\n\n"
                                    "Structure (ALL 5 sections required):\n"
                                    "1. **Executive Summary** (4-6 sentences) — headline finding with specific numbers (totals, averages, top items)\n"
                                    "2. **Key Numbers** (5-10 bullets) — concrete figures with their meaning. Use business terms, not raw column names. Skip ID columns (those ending in _id/id/code/no.) and dates.\n"
                                    "3. **Trends & Comparisons** (3-5 sentences) — patterns over time, distribution insights, concentration\n"
                                    "4. **Notable Anomalies** (2-4 bullets) — outliers, surprises, missing data\n"
                                    "5. **Recommended Next Steps** (3-5 bullets) — concrete actions\n\n"
                                    "CRITICAL RULES:\n"
                                    "- Compute numbers FROM the data yourself (sums, averages, rankings). Don't just list column names.\n"
                                    "- Respond in the same language as the user's question.\n"
                                    "- NEVER say 'I've completed your request' / 'Here is the artifact' / 'The data has been retrieved'. Start directly with the analysis.\n"
                                    "- NEVER just describe the data structure.\n\n"
                                    f"=== PRE-AGGREGATED STATISTICS (all {len(_last_rows)} rows) ===\n"
                                    f"{_preagg or '(no aggregates)'}\n\n"
                                    f"=== COLUMN SUMMARY ===\n"
                                    f"{_build_column_summary(_last_rows) or '(none)'}\n\n"
                                    f"=== DATA ({len(_rows_payload)} of {len(_last_rows)} rows shown) ===\n"
                                    f"{_rows_json}\n\n"
                                    "Write the report now. Use the 5 sections above. Compute numbers FROM the data, do not invent them."
                                )
                                _forced_synth = await _deferred_synth_call(
                                    _forced_prompt,
                                    [{"role": "user", "content": user_content}],
                                )
                                _forced_text = (_forced_synth.get("content", "") or "").strip()
                                # Validate: don't accept another apology/placeholder
                                _is_bad = (
                                    not _forced_text
                                    or len(_forced_text) < 100
                                    or _APOLOGY_PATTERN_RE.search(_forced_text)
                                    or _BOUNCE_BACK_PATTERN_RE.search(_forced_text)
                                )
                                if not _is_bad and len(_forced_text) > len(_synth_prose):
                                    _deferred_synth.assistant_content = _forced_text
                                    logger.info(
                                        "v3 forced synthesis produced %d chars (was %d)",
                                        len(_forced_text), len(_synth_prose),
                                    )
                                    _synth_prose = _forced_text
                                elif _is_bad:
                                    logger.info(
                                        "v3 forced synthesis produced bad answer "
                                        "(len=%d, apology=%s), will retry in Tier 2",
                                        len(_forced_text),
                                        bool(_APOLOGY_PATTERN_RE.search(_forced_text or "")),
                                    )
                            except Exception as _forced_err:
                                logger.warning(
                                    "v3 forced synthesis failed (non-fatal): %s",
                                    _forced_err,
                                )

                        # --- Tier 2: Second LLM synthesis attempt (more explicit) ---
                        # If after Tier 1 forced synthesis, prose is STILL too short,
                        # try one more time with even more explicit instructions.
                        # We ALWAYS use the LLM — never hardcoded answers — because
                        # different users have different databases/schemas.
                        _synth_prose = (_deferred_synth.assistant_content or "").strip()
                        if len(_synth_prose) < 150 and _last_rows:
                            logger.info(
                                "v3 synthesis STILL too short (%d chars) after Tier 1, "
                                "attempting Tier 2 LLM synthesis (conv=%s)",
                                len(_synth_prose), conversation_id,
                            )
                            try:
                                _tier2_answer = await _force_llm_synthesis(
                                    user_content,
                                    _last_rows,
                                    synth_call_fn=_deferred_synth_call,
                                    conversation_history=llm_messages,
                                    max_retries=2,
                                )
                                if len(_tier2_answer) > len(_synth_prose):
                                    _deferred_synth.assistant_content = _tier2_answer
                                    logger.info(
                                        "v3 Tier 2 LLM synthesis: %d chars (was %d)",
                                        len(_tier2_answer), len(_synth_prose),
                                    )
                            except Exception as _tier2_err:
                                logger.warning(
                                    "v3 Tier 2 LLM synthesis failed (non-fatal): %s",
                                    _tier2_err,
                                )
                        if _deferred_synth.report_card_payload is not None:
                            _payload_dump = _deferred_synth.report_card_payload.model_dump()
                            # FIX 2026-08-22: only create an artifact card when
                            # the user explicitly asked for a file deliverable.
                            # For simple data questions the synthesis text IS
                            # the answer — no card needed.
                            if _orch_doc_format:
                                _deferred_task = _start_finalize_offloaded(
                                    db,
                                    {
                                        "conversation_id": conversation_id,
                                        "agent_name": agent_name,
                                        "user_message": user_content,
                                        "source": _deferred_answer.get("source_name"),
                                        "sql": _deferred_answer.get("sql"),
                                        "payload": _deferred_synth.report_card_payload,
                                        "message_id": assistant_msg_id,
                                    },
                                )
                                # FIX 2026-08-22: cap the offloaded finalize wait so
                                # the SSE stream always closes. Without this, if the
                                # sandbox container hangs the "Finalizing your answer"
                                # activity step stays loading forever and nothing gets
                                # persisted to the database.
                                _DEFERRED_FINALIZE_TIMEOUT_S = 120
                                try:
                                    async with asyncio.timeout(_DEFERRED_FINALIZE_TIMEOUT_S):
                                        async for _hb in _emit_tool_progress_while_waiting(
                                            _deferred_task,
                                            [{
                                                "tool_call_id": "finalize-artifact-deferred",
                                                "tool_name": "create_artifact",
                                                "args_str": "",
                                                "args": {},
                                            }],
                                        ):
                                            yield _hb
                                    _artifact_row, _file_exports = _deferred_task.result()
                                except asyncio.TimeoutError:
                                    logger.warning(
                                        "v3 deferred finalize timed out after %ds "
                                        "(sandbox render stuck). Continuing without "
                                        "file export. (conv=%s)",
                                        _DEFERRED_FINALIZE_TIMEOUT_S, conversation_id,
                                    )
                                    _deferred_task.cancel()
                                    _artifact_row, _file_exports = None, {}
                                except Exception as _deferred_err:
                                    logger.error(
                                        "v3 deferred finalize failed: %s (conv=%s)",
                                        _deferred_err, conversation_id,
                                    )
                                    _artifact_row, _file_exports = None, {}
                                # Re-serialize AFTER finalize so user_signal reflects
                                # any mutation (e.g. "export_docx").
                                _payload_dump = _deferred_synth.report_card_payload.model_dump()
                            else:
                                _artifact_row, _file_exports = None, {}
                                logger.info(
                                    "DEFERRED FINALIZE: skipped artifact creation "
                                    "(no file intent) for conv=%s",
                                    conversation_id,
                                )
                            # Attach the card payload + artifact to the answer
                            # dataset's tool-call record (the frontend MessageBubble
                            # reads report_card_payload from the persisted call).
                            _tc_record = next(
                                (
                                    r for r in tool_calls_for_frontend
                                    if r.get("id") == _deferred_answer.get("tool_call_id")
                                ),
                                None,
                            )
                            if _tc_record is None:
                                # Fallback: attach to the last answer-tagged call
                                # (the smart retry may have re-executed the query).
                                _tc_record = next(
                                    (
                                        r for r in reversed(tool_calls_for_frontend)
                                        if isinstance(r.get("results"), dict)
                                        and r.get("results", {}).get("query_purpose") == "answer"
                                    ),
                                    None,
                                )
                            if _tc_record is not None:
                                # Attach INSIDE results — MessageBubble's
                                # reportToolResult reads tc.results.report_card_payload,
                                # matching every legacy eager path
                                # (result["report_card_payload"]). The first deferred
                                # implementation wrote at the record top level, so the
                                # ReportCard never rendered (final-answer layout bug,
                                # 2026-08-21).
                                _tc_results = _tc_record.get("results")
                                if not isinstance(_tc_results, dict):
                                    _tc_results = {}
                                    _tc_record["results"] = _tc_results
                                _tc_results["report_card_payload"] = _payload_dump
                                _tc_results["synthesis_text"] = _deferred_synth.assistant_content
                                # Bubble-text companion: when the turn produced
                                # no prose (or the strips removed it all), show
                                # the synthesis text as the bubble so the card
                                # never arrives over a blank message. The
                                # empty-bubble guarantee further below only
                                # catches the no-card case.
                                #
                                # FIX 2026-08-22: also replace fallback placeholder
                                # text (e.g. "Data retrieved (1 rows):" from the
                                # post-loop fallback, or the generic "I gathered
                                # some information…" message). The synthesis ran
                                # and produced real prose — the user should see it,
                                # not the placeholder. Without this override, the
                                # synthesis text is attached only to
                                # _tc_results["synthesis_text"] (which MessageBubble
                                # ignores), and the bubble shows the stale raw-rows
                                # fallback, so the report card appears to "vanish"
                                # once the stream ends and the user re-reads the
                                # conversation.
                                _synth_text = (_deferred_synth.assistant_content or "").strip()
                                _acc_text = (accumulated_content or "").strip()
                                _is_fallback_placeholder = (
                                    not _acc_text
                                    or _acc_text == _GENERIC_EMPTY_CONTENT_FALLBACK.strip()
                                    or _acc_text.startswith("(The requested")
                                    or _acc_text.startswith("Data retrieved (")
                                    or _acc_text.startswith("Analyzing ")
                                    or _acc_text.startswith("I gathered some information")
                                    or _acc_text.startswith("Based on the analysis of")
                                    or _acc_text.startswith("I've completed your request")
                                    or _acc_text.startswith("Your deliverable")
                                    or _acc_text.startswith("Here is the artifact")
                                    # 2026-08-26: the post-loop strips can
                                    # leave a SHORT residue that is neither
                                    # empty nor a recognized prefix. It must
                                    # not block the rich synthesis prose.
                                    or _is_weak_strip_residue(_acc_text, _synth_text)
                                )
                                # Mirror the empty-bubble guard: never clobber
                                # good prose with artifact boilerplate.
                                if (
                                    _synth_text
                                    and _is_fallback_placeholder
                                    and not _synth_text.startswith(
                                        ("I've completed your request",
                                         "Your deliverable",
                                         "Here is the artifact")
                                    )
                                ):
                                    accumulated_content = _deferred_synth.assistant_content
                                    try:
                                        yield (
                                            'data: '
                                            + json.dumps({
                                                "type": "content_replace",
                                                "content": accumulated_content,
                                            })
                                            + '\n\n'
                                        )
                                    except Exception as _bubble_yield_err:
                                        logger.debug(
                                            "v3 deferred bubble yield failed "
                                            "(client gone?): %s",
                                            _bubble_yield_err,
                                        )
                                if _file_exports:
                                    _tc_results["file_exports"] = _file_exports
                                    _primary_fmt = next(iter(_file_exports))
                                    _export = _file_exports[_primary_fmt]
                                    _export_aid = _export.get("artifact_id")
                                    if _export_aid:
                                        _tc_results["artifact_id"] = _export_aid
                                        if _export_aid not in artifact_ids:
                                            artifact_ids.append(_export_aid)
                                if _artifact_row is not None and not _file_exports:
                                    _tc_results["artifact_id"] = _artifact_row.id
                                    if _artifact_row.id not in artifact_ids:
                                        artifact_ids.append(_artifact_row.id)
                            # Emit artifact_created SSE (same shape as mid-loop).
                            _deferred_aid = None
                            if _file_exports:
                                _deferred_aid = (
                                    next(iter(_file_exports.values()), {}).get("artifact_id")
                                    or (_artifact_row.id if _artifact_row is not None else None)
                                )
                            elif _artifact_row is not None:
                                _deferred_aid = _artifact_row.id
                            if _deferred_aid:
                                _live_art = {
                                    "artifact_id": _deferred_aid,
                                    "version_id": (
                                        getattr(_artifact_row, "version_id", None)
                                        if _artifact_row is not None else None
                                    ),
                                    "version_number": (
                                        getattr(_artifact_row, "version_number", None)
                                        if _artifact_row is not None else None
                                    ),
                                    "file_url": (
                                        getattr(_artifact_row, "file_url", None)
                                        if _artifact_row is not None else None
                                    ),
                                    "preview_url": (
                                        getattr(_artifact_row, "preview_url", None)
                                        if _artifact_row is not None else None
                                    ),
                                    "title": (
                                        getattr(_artifact_row, "title", "")
                                        if _artifact_row is not None else ""
                                    ),
                                    "type": (
                                        getattr(_artifact_row, "type", "")
                                        if _artifact_row is not None else ""
                                    ),
                                    "file_name": (
                                        getattr(_artifact_row, "file_name", "")
                                        if _artifact_row is not None else ""
                                    ),
                                    "mime_type": (
                                        getattr(_artifact_row, "mime_type", "")
                                        if _artifact_row is not None else ""
                                    ),
                                    "file_size": (
                                        getattr(_artifact_row, "file_size", None)
                                        if _artifact_row is not None else None
                                    ),
                                    "has_preview": bool(
                                        getattr(_artifact_row, "has_preview", False)
                                        if _artifact_row is not None else False
                                    ),
                                }
                                yield (
                                    f'data: {json.dumps({"type": "artifact_created", "artifact": _live_art})}\n\n'
                                )
                            logger.info(
                                "v3 DEFERRED deliverable: card emitted once post-loop (conv=%s, rows=%d)",
                                conversation_id, len(_last_rows or []),
                            )
                    except Exception as deferred_err:
                        logger.warning(
                            "v3 deferred deliverable failed (non-fatal) for conv=%s: %s",
                            conversation_id, deferred_err,
                        )

        # ── Empty-bubble guarantee (post-strip, post-deferred) ────────────
        # The strips above (promise + internal-reference) can remove ALL
        # accumulated prose when the model's only output was narration —
        # exactly the failing traces of 2026-08-21 ("Let me query the
        # warehouse… Let me check the tables…" as the entire response).
        # The earlier post-loop fallback (~line 11108) skips itself when
        # content_streamed=True ("the streamed deltas are the answer"),
        # but those deltas were the narration we just stripped. Guarantee:
        # NEVER persist an empty final bubble — a turn must always end
        # with a real message (answer, card intro, or honest fallback).
        #
        # FIX 2026-08-23: synthesis-aware fallback. When the deferred
        # deliverable path ran above and produced synthesis text + an
        # artifact (PPT/DOCX/PDF), we MUST NOT fall back to
        # `_choose_fallback → _artifact_aware_fallback` which would
        # surface the misleading "I've completed your request. Here's
        # the artifact: ..." text — that hides the actual analysis.
        # Instead, prefer (in order):
        #   1. `_deferred_synth.assistant_content` (the LLM's narrative)
        #   2. A data-rows narrative built from `_last_rows` (the
        #      comprehensive-table fallback, NOT a generic apology)
        #   3. Only if BOTH are empty/missing → `_choose_fallback`
        #      (which now will at least mention the artifact).
        # DIAGNOSTIC 2026-08-25: capture state at empty-bubble entry
        try:
            _lr_count = len(_last_rows) if "_last_rows" in locals() and _last_rows else 0
            _tc_count = len(tool_calls_for_frontend)
            _ac_len = len(accumulated_content) if accumulated_content else 0
            _ds_len = (
                len(_deferred_synth.assistant_content)
                if ("_deferred_synth" in locals() and _deferred_synth is not None
                    and getattr(_deferred_synth, "assistant_content", None))
                else 0
            )
            _ond = _orch_no_data[0] if "_orch_no_data" in locals() else None
            _anywr = _any_ask_data_with_rows if "_any_ask_data_with_rows" in locals() else None
            logger.info(
                "DIAG v3 empty-bubble entry: conv=%s, last_rows=%d, "
                "tool_calls=%d, accumulated=%d, deferred_synth=%d, "
                "orch_no_data=%s, any_ask_data_with_rows=%s",
                conversation_id, _lr_count, _tc_count, _ac_len, _ds_len,
                _ond, _anywr,
            )
        except Exception as _diag_err:
            logger.warning("DIAG v3 empty-bubble entry failed: %s", _diag_err)
        if not (accumulated_content or "").strip():
            _synth_for_bubble = ""
            try:
                if "_deferred_synth" in locals() and _deferred_synth is not None:
                    _synth_for_bubble = (
                        getattr(_deferred_synth, "assistant_content", "") or ""
                    ).strip()
            except Exception:
                _synth_for_bubble = ""
            if _synth_for_bubble and not _synth_for_bubble.startswith(
                ("I've completed your request", "Your deliverable", "Here is the artifact")
            ):
                # Synthesis produced real prose — use it. This is the
                # same content the chat bubble should show.
                accumulated_content = _synth_for_bubble
                logger.info(
                    "v3 empty-bubble guarantee: using deferred synthesis text "
                    "(conv=%s, len=%d)",
                    conversation_id, len(accumulated_content),
                )
            elif "_last_rows" in locals() and _last_rows:
                # No (or weak) synthesis text but we DO have data rows.
                # Use the LLM to write a comprehensive analysis — never
                # hardcoded, because different users have different databases.
                try:
                    _llm_answer = await _force_llm_synthesis(
                        user_content,
                        _last_rows,
                        synth_call_fn=_deferred_synth_call,
                        conversation_history=llm_messages,
                        max_retries=2,
                    )
                    if len(_llm_answer) > 50:
                        accumulated_content = _llm_answer
                        logger.info(
                            "v3 empty-bubble: LLM answer (%d chars, conv=%s)",
                            len(_llm_answer), conversation_id,
                        )
                    else:
                        # LLM couldn't produce a real answer either
                        accumulated_content = _data_rows_fallback(
                            tool_calls_for_frontend, user_content=user_content,
                        )
                except Exception as _synth_exc:
                    # 2026-08-26: log the actual exception so we can
                    # diagnose why the synthesis keeps failing instead
                    # of silently falling through to the placeholder.
                    logger.warning(
                        "v3 empty-bubble: _force_llm_synthesis failed "
                        "(conv=%s, rows=%s): %s",
                        conversation_id,
                        len(_last_rows) if "_last_rows" in locals() else "?",
                        _synth_exc,
                    )
                    accumulated_content = _data_rows_fallback(
                        tool_calls_for_frontend, user_content=user_content,
                    )
            if not (accumulated_content or "").strip():
                try:
                    accumulated_content = _choose_fallback(
                        tool_calls_for_frontend, _orch_created,
                        user_content=user_content,
                    )
                except Exception as _guarantee_err:
                    logger.warning(
                        "v3 empty-bubble guarantee fallback failed: %s",
                        _guarantee_err,
                    )
                    accumulated_content = _GENERIC_EMPTY_CONTENT_FALLBACK
                logger.info(
                    "v3 empty-bubble guarantee fired (conv=%s, streamed=%s, len=%d)",
                    conversation_id,
                    bool(locals().get("content_streamed")),
                    len(accumulated_content),
                )
            else:
                logger.info(
                    "v3 empty-bubble guarantee: synthesis-aware replacement "
                    "(conv=%s, streamed=%s, len=%d)",
                    conversation_id,
                    bool(locals().get("content_streamed")),
                    len(accumulated_content),
                )
            try:
                if locals().get("content_streamed"):
                    # Swap the (already-blanked or stale narration) live
                    # bubble for the guarantee text — same content_replace
                    # pattern as every nudge site.
                    yield (
                        'data: '
                        + json.dumps({
                            "type": "content_replace",
                            "content": accumulated_content,
                        })
                        + '\n\n'
                    )
                else:
                    yield (
                        'data: '
                        + json.dumps({
                            "type": "delta",
                            "content": accumulated_content,
                        })
                        + '\n\n'
                    )
            except Exception as _yield_err:
                logger.debug(
                    "v3 empty-bubble guarantee yield failed (client gone?): %s",
                    _yield_err,
                )

        # ── Apology-guard (post-loop, deterministic) ──────────────────────
        # The in-loop check above should have caught an apology accompanied by
        # usable data and forced a real re-synthesis. If it still slipped
        # through (the forced retry ALSO apologized, or the loop ended before
        # the net could fire), deterministically swap the apology/bounce-back
        # for a data-aware message so the user NEVER sees "I had trouble
        # putting it all together" or "I retrieved N rows… you can ask me for
        # a summary" when rows were actually retrieved.
        _is_bounce_back = _BOUNCE_BACK_PATTERN_RE.search(accumulated_content or "")
        _is_apology = _APOLOGY_PATTERN_RE.search(accumulated_content or "")
        if (_is_bounce_back or _is_apology) and _has_data_rows(tool_calls_for_frontend):
            try:
                # Use LLM to write a comprehensive answer instead of
                # _data_rows_fallback. Different databases need the LLM
                # to interpret data meaningfully.
                _llm_ans = await _force_llm_synthesis(
                    user_content,
                    _last_rows if "_last_rows" in locals() else [],
                    synth_call_fn=_deferred_synth_call,
                    conversation_history=llm_messages,
                    max_retries=2,
                )
                if len(_llm_ans) > 50:
                    accumulated_content = _llm_ans
                else:
                    accumulated_content = _data_rows_fallback(
                        tool_calls_for_frontend, user_content=user_content,
                    )
            except Exception as _apology_err:
                logger.warning("v3 apology/bounce-back fallback failed: %s", _apology_err)
                try:
                    accumulated_content = _choose_fallback(
                        tool_calls_for_frontend, _orch_created,
                        user_content=user_content,
                    )
                except Exception:
                    accumulated_content = _GENERIC_EMPTY_CONTENT_FALLBACK
            logger.info(
                "v3 %s (post-loop): swapped for data-aware message "
                "(conv=%s, len=%d)",
                "bounce-back" if _is_bounce_back else "apology-guard",
                conversation_id,
                len(accumulated_content),
            )
            try:
                if locals().get("content_streamed"):
                    yield (
                        'data: '
                        + json.dumps({
                            "type": "content_replace",
                            "content": accumulated_content,
                        })
                        + '\n\n'
                    )
                else:
                    yield (
                        'data: '
                        + json.dumps({
                            "type": "delta",
                            "content": accumulated_content,
                        })
                        + '\n\n'
                    )
            except Exception:
                pass

        # Save the final assistant message
        assistant_msg = {
            "id": assistant_msg_id, "role": "assistant",
            "content": accumulated_content,
            "created_date": datetime.now(timezone.utc).isoformat(),
        }
        # Kimi/GPT-style citations: collect the data sources the tool loop
        # actually queried this turn (source_id/source_name from tool
        # results). Attached so the frontend can render source chips.
        try:
            _legacy_sources = _extract_citations_from_tool_calls(tool_calls_for_frontend)
            if _legacy_sources:
                assistant_msg["sources"] = _legacy_sources
        except Exception:  # noqa: BLE001 — citations must never break the stream
            pass
        if tool_calls_for_frontend:
            # 2026-08-28: cap the PERSISTED tool history so a long CAD/data
            # turn (local models re-emit whole build batches — a bolt+nut
            # can produce 150+ entries) doesn't flood the NEXT turn's LLM
            # context and confuse the model about the live scene state.
            # The live SSE stream still shows the full list during the turn.
            assistant_msg["tool_calls"] = tool_calls_for_frontend[-50:]
        # Attach artifact_ids so the frontend can render ArtifactPreviewCard
        if artifact_ids:
            assistant_msg["artifact_ids"] = artifact_ids
        # Attach activity_steps so they survive page refresh
        if _activity_steps:
            assistant_msg["activity_steps"] = _activity_steps
        # Attach the typed live-activity feed so past turns render their
        # collapsed summary on reload (LiveActivityStream).
        if _live_events:
            assistant_msg["live_events"] = _live_events
        # Surface create_artifact results as artifacts
        _artifacts = _collect_artifact_results(
            tool_calls_for_frontend, assistant_msg_id, conversation_id, db,
        )
        if _artifacts:
            assistant_msg["artifacts"] = _artifacts
        # Derive and attach the execution trace (Reasoning & actions)
        assistant_msg["trace"] = _derive_trace_from_response(
            assistant_content, tool_calls_for_frontend,
        )
        # P0: persist reasoning as a separate key (not mixed into content)
        # so context compaction does not feed it back to the model.
        if reasoning_acc:
            assistant_msg["reasoning"] = "".join(reasoning_acc)

        # ── Final-write dedupe ─────────────────────────────────────────
        # The stream checkpoints above mutate ``messages[:] = base`` on
        # every partial commit, so by the time we reach here ``messages``
        # contains one trailing empty assistant placeholder per checkpoint.
        # Each has empty content + the same accumulated ``tool_calls``.
        # Before appending the authoritative final, drop those trailing
        # placeholders so the user sees exactly ONE assistant bubble for
        # this turn (not one per iteration). Only touches the trailing
        # run — earlier user/system messages and assistant messages with
        # real content are preserved.
        while (
            messages
            and messages[-1].get("role") == "assistant"
            and not messages[-1].get("content")
        ):
            messages.pop()

        messages.append(assistant_msg)

        # CRITICAL: rebind conv.messages to a NEW list object so SQLAlchemy
        # detects the change for the JSON column. The checkpoint block
        # above (lines 1991-2016) rebinds `conv.messages` to a different
        # list object (the `base` snapshot), so simply re-assigning
        # `messages.append(...)` does NOT propagate to conv.messages —
        # the in-memory `messages` closure variable and `conv.messages`
        # end up pointing at different list objects. We rebuild the
        # final list here and assign it explicitly to ensure the
        # `done` event's conversation payload reflects the actual
        # stored state.
        conv.messages = list(messages)
        conv.updated_date = datetime.now(timezone.utc)
        # Offload the final commit to a worker thread: the DB write can take
        # tens of milliseconds (JSON column + flush), and blocking the event
        # loop here starves the SSE heartbeat pings exactly when the client
        # most needs them (right before `done`). NEVER re-raise on failure —
        # a persistence error must not abort the stream before `done` is
        # emitted; the user still sees their answer, and the row can be
        # reconciled later.
        def _final_commit() -> None:
            conv.messages = list(messages)
            db.commit()

        try:
            await asyncio.to_thread(_final_commit)
        except Exception as _commit_err:
            logger.error("v3 stream final commit failed (non-fatal): %s", _commit_err)
            try:
                db.rollback()
            except Exception:
                pass
        # The checkpoint block above may have expunged the instance;
        # re-query a fresh one so the final to_dict() reflects the
        # freshly-committed row.
        try:
            conv = db.query(AgentConversation).filter(
                AgentConversation.id == conversation_id,
            ).first() or conv
        except Exception as _refresh_err:
            logger.warning("v3 stream final refresh failed: %s", _refresh_err)

        # Fire-and-forget memory extraction
        if agent_app_id and len(messages) >= 4:
            asyncio.create_task(_bg_extract_memories(
                agent_app_id, list(messages), user.id if user else None,
                project_id=getattr(conv, "project_id", None),
            ))

        # P3 post-turn memory consolidation — extract durable user facts via
        # LLM and merge into the OHMO workspace's user.md. Gated behind
        # OHMO_MEMORY_CONSOLIDATION_ENABLED (default OFF). Best-effort: the
        # function never raises, so a failure here cannot break the SSE
        # stream. Runs as a fire-and-forget task (sync LLM + sync OHMO I/O
        # offloaded via to_thread to keep the event loop clean).
        try:
            if getattr(settings, "OHMO_MEMORY_CONSOLIDATION_ENABLED", False):
                import asyncio as _asyncio
                _user_msg = next(
                    (m.get("content", "") for m in messages if m.get("role") == "user"),
                    "",
                )
                _asyncio.create_task(_asyncio.to_thread(
                    consolidate_turn_memory,
                    _user_msg,
                    assistant_content or "",
                ))
        except Exception as _mc_hook_err:
            logger.warning(
                "v3 stream: memory consolidation hook failed (non-fatal): %s",
                _mc_hook_err,
            )

        # QUALITY_EVAL (Part 2 — Gap Analysis): run standalone quality
        # eval on the non-FSM v3 streaming path. Gated by
        # QUALITY_EVAL_ALL_PATHS.  When the text is revised, the done
        # event carries the updated assistant_content.
        # (Fixed 2026-08-17): the previous version called
        # evaluate_response_quality() synchronously INSIDE the async SSE
        # generator.  That blocked the event loop for the whole LLM eval
        # (up to 120s per call), so _sse_with_heartbeat couldn't emit its
        # 5s pings and proxies/browsers timed out — the client saw
        # "Sorry, the connection was interrupted."  Now offloaded to a
        # worker thread with a 15s ceiling and eval-only (no re-gen).
        try:
            if getattr(settings, "QUALITY_EVAL_ALL_PATHS", True):
                from app.services.synexia.quality_eval import evaluate_response_quality

                async def _run_qe() -> object:
                    def _sync_qe() -> object:
                        return evaluate_response_quality(
                            user_message=user_content or "",
                            assistant_text=assistant_content,
                            max_iterations=0,  # eval-only, never re-generate
                        )
                    return await asyncio.to_thread(_sync_qe)

                _qe_result = await asyncio.wait_for(_run_qe(), timeout=15.0)
                if _qe_result.final_text and _qe_result.final_text != assistant_content:
                    assistant_content = _qe_result.final_text
                    # Multi-iter content re-sync (2026-08-20):
                    # Quality eval revised `assistant_content` POST-LOOP.
                    # The accumulator's LAST entry holds the pre-revision
                    # final-iter content — replace it so the revision is
                    # preserved while earlier iterations stay intact.
                    # Then re-compute `accumulated_content` and sync the
                    # in-memory `assistant_msg["content"]` so the `done`
                    # event's conversation payload reflects the revision
                    # (DB commit at the final-write block already happened
                    # with pre-revision content; this is a pre-existing
                    # inconsistency, not introduced by this fix).
                    if _v3_iter_contents:
                        _v3_iter_contents[-1] = assistant_content
                    else:
                        _v3_iter_contents.append(assistant_content)
                    accumulated_content = "\n\n".join(c for c in _v3_iter_contents if c).strip()
                    if isinstance(assistant_msg, dict):
                        assistant_msg["content"] = accumulated_content
                # Emit quality eval verdict as SSE event
                yield f'data: {json.dumps({"type": "quality_eval", "quality_eval": _qe_result.to_dict()})}\n\n'
        except asyncio.TimeoutError:
            logger.warning("v3 stream quality eval timed out after 15s (non-fatal)")
        except Exception as _qe_err:
            logger.warning("v3 stream quality eval failed (non-fatal): %s", _qe_err)

        # --- Experience layer (Phase A/B): recipe + profile + cache store ---
        # BUG FIX (post-tool offload, follows f435531): get_embedding() inside
        # _store_turn_cache is a sync LLM API call (5-60s); running it inline
        # on the event loop starved the SSE heartbeat pings in
        # _sse_with_heartbeat() so the `done` frame below was never emitted
        # before nginx/proxy idle-killed the connection. Offload the whole
        # experience hook to a worker thread, bounded by a ceiling so a stalled
        # embedding service fails over gracefully instead of hanging the stream.
        # The offloaded work opens its own DB session (SQLAlchemy Session is
        # not thread-safe across threads; see the established pattern at
        # line 5522). Best-effort: any failure is logged and skipped, so the
        # `done` frame below always fires.
        try:
            def _experience_work() -> None:
                from app.database import SessionLocal
                _record_turn_experience(
                    agent_app_id=agent_app_id,
                    user_id=getattr(user, "id", None),
                    user_content=user_content,
                    assistant_content=assistant_content,
                    tool_sequence=tool_calls_for_frontend,
                    iterations=len(tool_calls_for_frontend or []),
                )
                cache_db = SessionLocal()
                try:
                    _store_turn_cache(
                        db=cache_db,
                        agent_app_id=agent_app_id,
                        user_id=getattr(user, "id", None),
                        user_content=user_content,
                        assistant_content=assistant_content,
                        artifact_ids=(
                            assistant_msg.get("artifact_ids")
                            if isinstance(assistant_msg, dict) else None
                        ),
                    )
                finally:
                    try:
                        cache_db.close()
                    except Exception:
                        pass

            await asyncio.wait_for(
                asyncio.to_thread(_experience_work),
                timeout=EXPERIENCE_HOOK_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "v3 stream experience hook timed out after %ss (non-fatal)",
                EXPERIENCE_HOOK_TIMEOUT_S,
            )
        except Exception as _xp_err:  # noqa: BLE001 — best-effort
            logger.warning("experience: turn-end hook failed (non-fatal): %s", _xp_err)

        # ── Always emit the `done` frame last ──
        # BUG FIX: the `done` frame is the only signal the frontend uses to
        # commit the assistant message; if any earlier step raised something
        # the outer try/excepts didn't catch (e.g. BaseException subclasses
        # like asyncio.CancelledError from a proxy timeout, or a generator
        # close mid-stream), the frontend would see a dropped connection
        # without ever receiving `done`. Wrap the post-tool section in a
        # try/finally so `done` is yielded regardless. (See f435531 — the
        # artifact-render fix already offloaded the long sync work; this
        # guards the remaining path.)
        try:
            _le_fd = _push_live_event("finalize_done", "finalize_done")
            if _le_fd:
                yield _le_fd
            # UX FIX (2026-08-24): When intermediate deltas were suppressed,
            # emit the final accumulated content as a single content_replace
            # BEFORE the done event. The frontend MessageBubble reads the
            # content field from `done` for the persisted bubble, but many
            # UI implementations only update live from streaming events —
            # without this explicit content_replace the user would see an
            # empty bubble even after the file is ready.
            if _suppress_chat_deltas:
                _final_bubble_text = (
                    accumulated_content or assistant_content or ""
                )
                if _final_bubble_text:
                    yield (
                        f'data: {json.dumps({"type": "content_replace", "content": _final_bubble_text})}\n\n'
                    )
            # ── Post-build dashboard verification gate ─────────────
            # Deterministic check AFTER a dashboard turn: if the build tool
            # ran this turn but NO dashboard_apps row exists for this
            # conversation, append a visible failure to the final content
            # instead of letting a confident-but-fake success story land.
            # (The "text report instead of dashboard" failure mode.)
            _dash_verify_msg = None
            try:
                _dash_verify_msg = verify_dashboard_build_produced_app(
                    db,
                    conversation_id,
                    _v3_executed_tool_names,
                    _dash_build_tool,
                )
            except Exception:  # noqa: BLE001 — gate must never break the stream
                _dash_verify_msg = None
            if _dash_verify_msg:
                _done_content = (
                    (accumulated_content or assistant_content or "")
                    + "\n\n"
                    + _dash_verify_msg
                )
                logger.warning(
                    "v3 stream: dashboard build verification FAILED "
                    "(conv=%s, tool=%s)",
                    conversation_id, _dash_build_tool,
                )
            else:
                _done_content = accumulated_content or assistant_content
            # ── Tier 1 auto-refine gate (2026-08-28) ──────────────────
            # If the build's own quality report was B/C and NO
            # update_fullstack_dashboard followed in the same turn, the agent
            # shipped a thin board on purpose — surface that to the user.
            _dash_q_msg = None
            try:
                from app.services.dashboard_turn_guard import (
                    verify_dashboard_quality_refined,
                )
                _dash_q_msg = verify_dashboard_quality_refined(
                    _dash_quality_worst, _dash_refined, _dash_quality_gaps,
                )
            except Exception:  # noqa: BLE001 — gate must never break the stream
                _dash_q_msg = None
            if _dash_q_msg:
                _done_content = (_done_content + "\n\n" + _dash_q_msg).strip()
                logger.warning(
                    "v3 stream: dashboard quality B/C shipped unrefined "
                    "(conv=%s, grade=%s, gaps=%s)",
                    conversation_id, _dash_quality_worst, _dash_quality_gaps,
                )
            # ── Per-turn invocation record (P1, 2026-08-29) ─────────────
            # One agent_invocations row per conversation turn: duration,
            # token usage, cost, trace id. Best-effort — never breaks the
            # done frame.
            try:
                from datetime import datetime as _dt2, timezone as _tz2
                from app.services.agent_invocations import record_invocation

                # Observability enrichment: which model served the turn and
                # how many tool calls it made (derived from the emitted
                # reasoning trace, where each tool_call step is one call).
                _inv_model = None
                try:
                    _inv_model = effective_llm.endpoint.model_id if effective_llm.endpoint else None
                except Exception:  # noqa: BLE001 — non-fatal
                    _inv_model = None
                _inv_trace = assistant_msg.get("trace", []) if isinstance(assistant_msg, dict) else []
                _tool_count = sum(
                    1 for _t in _inv_trace
                    if isinstance(_t, dict) and _t.get("type") == "tool_call"
                ) or None

                record_invocation(
                    db,
                    agent_app_id=agent_app_id,
                    conversation_id=conversation_id,
                    user_id=getattr(user, "id", None),
                    invocation_type="conversation",
                    trigger="user",
                    input_message=user_content,
                    status="completed",
                    assistant_content=_done_content,
                    started_at=_turn_started,
                    completed_at=_dt2.now(_tz2.utc),
                    duration_ms=max(0, int((_dt2.now(_tz2.utc) - _turn_started).total_seconds() * 1000)),
                    trace_id=getattr(_qe_result, "trace_id", None) if "_qe_result" in locals() else None,
                    model_name=_inv_model,
                    tool_call_count=_tool_count,
                )
            except Exception as _inv_err:  # noqa: BLE001 — non-fatal
                logger.warning("v3 stream: invocation record failed (non-fatal): %s", _inv_err)
            yield f'data: {json.dumps({"type": "done", "content": _done_content, "trace": assistant_msg.get("trace", []), "conversation": conv.to_dict()})}\n\n'
        except (GeneratorExit, StopIteration):
            raise
        except Exception as _done_err:
            logger.error(
                "v3 stream: failed to serialize `done` frame (non-fatal): %s",
                _done_err,
            )
            # Emit a minimal fallback `done` so the frontend at least unblocks.
            try:
                yield f'data: {json.dumps({"type": "done", "content": accumulated_content or assistant_content or "", "trace": [], "conversation": {}})}\n\n'
            except Exception:
                logger.error("v3 stream: fallback done frame also failed")
        finally:
            # Clean up the steer bus for this conversation (best-effort).
            try:
                _discard_steer(conversation_id)
            except Exception as _discard_err:
                logger.warning("v3 stream: steer discard failed (non-fatal): %s", _discard_err)
            # T18: clear the dashboard-intent flag set at event_stream entry.
            try:
                reset_dashboard_intent(_dash_intent_token)
            except Exception as _reset_err:
                logger.warning("v3 stream: reset dashboard intent failed (non-fatal): %s", _reset_err)

    return StreamingResponse(
        _sse_with_heartbeat(_guarantee_done(_disconnect_safe_stream(event_stream))),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            # Bug 3 fix: see comment on the other StreamingResponse
            # above. Same keep-alive header for the legacy (non-FSM)
            # streaming path.
            "Connection": "keep-alive",
        },
    )
