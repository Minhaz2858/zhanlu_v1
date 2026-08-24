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
from app.services.intent.export_intent import detect_export_intent
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


def _choose_fallback(
    tool_calls_for_frontend: list[dict],
    orch_created: list[dict],
    user_content: str = "",
) -> str:
    """Choose the best fallback message based on what was produced this turn.

    Priority:
    1. If data-agent report cards exist → ``_data_summary_fallback``
    2. If any artifact exists → ``_artifact_aware_fallback``
    3. If user asked for a dashboard → ``_DASHBOARD_REDIRECT_FALLBACK``
    4. Otherwise → ``_GENERIC_EMPTY_CONTENT_FALLBACK``
    """
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
        return _data_rows_fallback(tool_calls_for_frontend)
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
    _is_file, _fmt = is_file_deliverable_request(user_content)
    if _is_file and _fmt:
        from app.services.file_turn_guard import build_file_disclosure
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


def _data_rows_fallback(tool_calls_for_frontend: list[dict]) -> str:
    """Direct fallback built from the actual retrieved rows (no LLM).

    Renders a clean markdown table (≤10 rows), hiding system columns
    (FENTRYID, FCUSTMATID, …) and all-empty columns. Never surfaces
    raw "Sample values: FENTRYID=…" junk.
    """
    from app.services.goal_contract import is_metadata_only_rows  # lazy
    for tc in (tool_calls_for_frontend or []):
        if tc.get("name") not in DATA_PRODUCING_TOOLS:
            continue
        result = tc.get("results")
        if not isinstance(result, dict):
            continue
        rows = result.get("rows")
        if not rows:
            continue
        src = result.get("source_name") or "the data source"
        if isinstance(rows, (list, tuple)):
            n = len(rows)
            # Flatten to dict rows
            dict_rows = [r for r in rows if isinstance(r, dict)]
            if not dict_rows:
                continue
            # Metadata-only rows (MIN_DATE/MAX_DATE/ENTRY_COUNT) are not
            # meaningful business data — show a targeted message instead.
            if is_metadata_only_rows(dict_rows):
                return (
                    f"The query against {src} returned only summary metadata "
                    f"(date range / row count) rather than actual business data. "
                    f"Please try rephrasing your request with more specific "
                    f"dimensions (e.g. by product, customer, or time period) "
                    f"so I can retrieve the detailed figures you need."
                )
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
            # FIX 2026-08-22: return a neutral placeholder instead of a
            # markdown table so the user doesn't see raw data mid-stream.
            # The synthesis step replaces this with the final narrative
            # (which may include a summary table).  The frontend
            # DataTableCard (gated by !isStreaming) still renders the
            # data after streaming if synthesis didn't run.
            return f"Analyzing {len(rows)} rows of data…"
    return _GENERIC_EMPTY_CONTENT_FALLBACK


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
    if not rows:
        return ""

    col_summary = _build_column_summary(rows)
    rows_sample = json.dumps(rows[:8], default=str, ensure_ascii=False)[:2000]

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
        if attempt == 0:
            prompt = (
                "You are a data analyst. The user asked: "
                f"\"{user_msg[:200]}\"\n\n"
                f"The database returned {len(rows)} rows of real business data.\n\n"
                "Write a COMPREHENSIVE 5-8 sentence analysis that:\n"
                "1. Restates what was asked and what data was found\n"
                "2. Summarizes key totals, figures, and trends (use SPECIFIC "
                "numbers from the data — compute sums, averages, top items)\n"
                "3. Identifies top performers, key patterns, or rankings\n"
                "4. Highlights any anomalies or notable findings\n"
                "5. Ends with one concrete follow-up suggestion\n\n"
                "CRITICAL RULES:\n"
                "- NEVER say 'I've completed your request' or 'Here is the "
                "artifact' or 'The data has been retrieved'\n"
                "- NEVER just describe the data structure or list column names\n"
                "- Your answer must be USEFUL even WITHOUT the attached file\n"
                "- Write REAL analysis with REAL numbers computed from the data\n"
                "- Respond in the same language as the user's question\n\n"
                f"{preagg_block}\n\n"
                f"Column summary (value ranges):\n{col_summary}\n\n"
                f"Data sample (first 8 rows):\n{rows_sample}"
            )
        else:
            # Attempt 2: even more explicit
            prompt = (
                "IMPORTANT: Your previous attempt did not produce a real "
                "analysis. You MUST write actual content now.\n\n"
                "The user asked: " + f"\"{user_msg[:200]}\"\n\n"
                f"You have {len(rows)} rows of real data. "
                "DO NOT describe the data structure. "
                "DO NOT say 'I have completed'. "
                "Instead, WRITE 5-8 SENTENCES OF ACTUAL ANALYSIS. "
                "Include specific numbers (totals, averages, top items). "
                "Treat this as if you are presenting findings to a manager.\n\n"
                f"{preagg_block}\n\n"
                f"Column summary:\n{col_summary}\n\n"
                f"Data sample:\n{rows_sample}\n\n"
                "Start your answer directly with the analysis. No preamble."
            )

        try:
            result = await synth_call_fn(prompt, conversation_history)
            text = (result.get("content", "") or "").strip()
            # Check it's a real answer, not another placeholder
            _is_bad = (
                not text
                or len(text) < 100
                or text.startswith("I've completed your request")
                or text.startswith("Here is the artifact")
                or text.startswith("The data has been retrieved")
                or _APOLOGY_PATTERN_RE.search(text)
                or _BOUNCE_BACK_PATTERN_RE.search(text)
            )
            if not _is_bad:
                return text
            logger.info(
                "force_llm_synthesis attempt %d produced bad answer "
                "(len=%d, apology=%s), retrying",
                attempt + 1, len(text),
                bool(_APOLOGY_PATTERN_RE.search(text)),
            )
        except Exception as e:
            logger.warning(
                "force_llm_synthesis attempt %d failed: %s",
                attempt + 1, e,
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
DATA_PRODUCING_TOOLS: frozenset = frozenset({
    "ask_data_agent",
    "execute_query",
    "execute_sql",
    "sql_query",
    "forecast_brief",
})

# FIX 2026-08-24: sub-agent tools that return a `prompt_text` formatted
# answer (not rows). Without harvesting their text, market-report turns
# collapse to the generic "I gathered some information" fallback because
# the row pipeline ignores them entirely.
TEXT_PRODUCING_TOOLS: frozenset = frozenset({
    "ask_perception",
    "ask_intelligence",
    "ask_diagnosis",
    "ask_decision",
    "ask_pricing",
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
    "ask_data_agent": 2,
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
    "ask_perception", "ask_intelligence", "ask_diagnosis",
    "ask_perception_intelligence_diagnosis", "ask_forecast_pricing",
    "ask_data_agent", "ask_orchestrator", "ask_weekly_report",
    "ask_pricing", "ask_forecast", "ask_report", "ask_rag_research",
    "ask_knowledge_graph", "ask_decision", "ask_macro_override",
    "ask_erp_kpi", "ask_erp_writeback",
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


def _persisted_result_str(tool_name: str, result: dict, conversation_id: str) -> str:
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
        _ctx_window = get_context_window(get_model())
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
            get_context_window(get_model())
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
    llm_messages.append({"role": "system", "content": "\n".join(lines)})
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
}

# ── Helper: format an activity_step description with optional args ──────
_VAR_PATTERN = "{}"

# Per-tool key priority: when filling a placeholder like {data_source_id},
# try these arg keys in order and use the first non-None non-empty value.
# Format: {tool_name: [(placeholder_name, [priority_arg_keys, ...]), ...]}
_ARG_KEY_PRIORITY: dict[str, list[tuple[str, list[str]]]] = {
    "ask_data_agent": [
        ("data_source_id", ["data_source_id", "question"]),
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
    if not args or _VAR_PATTERN not in template:
        return template
    try:
        priorities = _ARG_KEY_PRIORITY.get(tool_name, [])
        prio_map: dict[str, list[str]] = {ph: keys for ph, keys in priorities}
        result = template
        # Find all {placeholder} occurrences and resolve them
        import re
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
# (F-prefixed uppercase like FMATERIALID, FREALQTY, etc.).
# NOTE: We intentionally do NOT strip generic table/column names since
# different databases use different naming conventions. Only strip clearly
# internal/technical identifiers that users wouldn't understand.
_SQL_HINT_PATTERN = re.compile(
    r"\[Schema:[^\]]*\]"            # [Schema: ...] blocks
    r"|\bF[A-Z][A-Z0-9_]+\b",      # F-prefixed internal IDs (FMATERIALID, etc.)
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
    tool_results = _gate_tool_results_from_frontend(tool_calls_for_frontend)
    if not tool_results:
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

        # Full skill catalog injection (dynamic discovery). Every agent
        # sees ALL available skills — not just its bound ones — so it can
        # autonomously discover and use any skill via the `Skill` meta-tool.
        # This replaces the former passive "Recommended Skills" hint that
        # only suggested skills to the user.  Best-effort, token-budgeted.
        if agent_app:
            try:
                from app.services.skills_loader import (
                    list_skills as loader_list_skills,
                    unified_search,
                )
                from app.services.skill_routing.catalog import build_catalog
                from app.services.skill_routing.post_router_hook import (
                    build_agent_forced_skill_directive,
                    post_router_pick,
                )

                all_skills = [s.to_dict() for s in loader_list_skills()]
                catalog_block = build_catalog(all_skills)
                if catalog_block:
                    # Contextual priority hint: surface skills whose
                    # name/description match the user's message first,
                    # so the agent knows where to start looking.
                    priority_hint = ""
                    forced_skill_directive = ""
                    if user_content:
                        bound_skills = set(agent_app.skills or [])
                        results = unified_search(user_content, limit=5, db=db)
                        # Reuse the search results for the post-router-hook
                        # strong-match check, avoiding a second
                        # ``unified_search`` call.  When the hook fires,
                        # inject a HARD directive telling the LLM to
                        # invoke the matched skill as its first action.
                        forced = post_router_pick(
                            user_content,
                            db=db,
                            candidates=results,
                        )
                        if forced:
                            forced_skill_directive = (
                                build_agent_forced_skill_directive(
                                    forced["skill_name"],
                                    score=forced.get("score"),
                                )
                            )
                        unbound = [
                            r for r in results
                            if r["name"] not in bound_skills
                        ][:3]
                        if unbound:
                            hints = "\n".join(
                                f"- **{r['name']}**: {r['description']}"
                                for r in unbound
                            )
                            priority_hint = (
                                "\n### Priority matches for this request\n"
                                "These skills look most relevant to the "
                                "user's message — consider loading them "
                                "first:\n"
                                f"{hints}\n"
                            )
                    system_prompt += forced_skill_directive
                    system_prompt += (
                        "\n\n## Skill Catalog (dynamic discovery)\n"
                        "You have access to ALL skills below — not just the "
                        "ones bound to you. Use the `Skill` tool to load or "
                        "execute any skill by name (e.g. "
                        '`Skill {command: "execute weekly-report-generation"}`). '
                        "`load_skill_body` is a SEPARATE tool from the `Skill` "
                        "dispatcher — call it directly with {name: '<skill-name>'} "
                        "to read a skill's full methodology. NEVER pass a tool "
                        "name (like 'load_skill_body') as a Skill command.\n"
                        f"{priority_hint}\n<available_skills>\n"
                        f"{catalog_block}\n</available_skills>"
                    )
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
                # P1-5: pass the user-selected model's context window so
                # the Bound Data Sources block auto-compresses for small
                # models (e.g. qwen3.6-27b 65k) and stays full for big
                # models (deepseek 128k, claude 200k).
                target_context_window=get_context_window(
                    user_model_v2 or get_model()
                ),
            )
        except Exception as e:
            logger.debug("Data source runtime prep failed (non-fatal): %s", e)
            data_ctx_extras = {}

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
            # Also fires in dashboard-capable projects (Ecisco BI /
            # `ecisco_bi_assistant`) when the agent speculatively loads
            # dashboard skills before being asked — the user just said "hi"
            # but the agent is in a BI project with a bound datasource.
            _tool_names = _tool_names_from_schemas(tools)
            _is_dash_project = bool(agent_name and (
                "ecisco_bi" in agent_name.lower()
                or "bi_assistant" in agent_name.lower()
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
                _v2_tool_names = _tool_names_from_schemas(tools)
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
                if (
                    _v2_build_tool in _dash_guard_names
                    or _v2_build_tool in _v2_tool_names
                ) and any(
                    n in dashboard_guard_blocked_tools()
                    for n in _dash_guard_names
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
                _result_str = _persisted_result_str(tool_name, result, conversation_id)
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
                    llm_messages, _batch_ids, _batch_names, conversation_id
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
                            "role": "system",
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
            _result_str = _persisted_result_str(tool_name, result, conversation_id)
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
                llm_messages, _batch_ids, _batch_names, conversation_id
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
) -> None:
    """Fire-and-forget background task to extract memories after a conversation.

    Uses an independent DB session so it doesn't interfere with the
    request-level session that has already closed. Failures are logged
    as warnings and silently swallowed — this must never break the chat.
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
        "max_tokens": getattr(settings, "LLM_SYNTH_MAX_TOKENS", 6144),
    }
    if not model_has_fixed_temperature(_model):
        payload["temperature"] = 0.7

    async def _try_call(trim_rows: bool = False) -> dict:
        _payload = dict(payload)
        if trim_rows:
            # Trim visible rows in user messages to first 30 to reduce context
            _payload["messages"] = [
                {**m, "content": m["content"][:8000]}
                if m.get("role") == "user" and isinstance(m.get("content"), str)
                else m
                for m in _payload["messages"]
            ]
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


def _estimate_input_tokens(
    messages: list[dict],
    tools: list[dict] | None = None,
) -> int:
    """Rough token estimate for messages + tool definitions.

    Uses ~3.5 chars/token (slightly conservative for mixed EN/CJK) and
    accounts for tool JSON schemas which are sent outside ``messages`` but
    still consume context tokens.  Adds a flat +200 overhead for system
    prompt structural tokens (role markers, special tokens, etc.).
    """
    total_chars = sum(
        len(m.get("content") or "") + len(m.get("role") or "")
        for m in messages if isinstance(m, dict)
    )
    msg_tokens = max(1, total_chars // 4)  # 4 chars/token for messages (role overhead is small)

    # Tool definitions: each tool schema is typically 200-600 tokens.
    # Use json-length / 3.5 as a rough estimate.
    tool_tokens = 0
    if tools:
        tool_chars = sum(len(str(t)) for t in tools)
        tool_tokens = max(0, tool_chars // 4)

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
        base_payload["tools"] = tools
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
                    reasoning = delta.get("reasoning_content", "")
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
# ECISCO_BI_RECIPE_LEARNING_ENABLED / ECISCO_BI_USER_PROFILE_ENABLED.
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

    if getattr(settings, "ECISCO_BI_RECIPE_LEARNING_ENABLED", False):
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

    if getattr(settings, "ECISCO_BI_USER_PROFILE_ENABLED", False) and user_id:
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

    Gated by ECISCO_BI_RESPONSE_CACHE_ENABLED. Only substantive answers
    are stored; the cache service itself enforces scope (shared for
    data-driven intents, per-user for conversational) and freshness
    guards. Best-effort: any failure is logged and skipped.
    """
    if not getattr(settings, "ECISCO_BI_RESPONSE_CACHE_ENABLED", False):
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

    All adjustments are best-effort and flag-gated (ECISCO_BI_*_ENABLED).
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
    if getattr(settings, "ECISCO_BI_RECIPE_LEARNING_ENABLED", False):
        try:
            from app.services.learning_graph import adjust_recipe_feedback

            adjust_recipe_feedback(agent_key, intent_class, rating)
        except Exception as _recipe_fb_err:  # noqa: BLE001 — best-effort
            logger.warning("feedback: recipe adjust failed (non-fatal): %s", _recipe_fb_err)

    # 3) Cache entry adjustment (Layer 2) — thumbs down evicts it.
    if getattr(settings, "ECISCO_BI_RESPONSE_CACHE_ENABLED", False):
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
    if getattr(settings, "ECISCO_BI_USER_PROFILE_ENABLED", False):
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

    messages = conv.messages or []
    user_content = body.get("content", "")
    user_role = body.get("role", "user")
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
    # When ECISCO_BI_RESPONSE_CACHE_ENABLED is on, attempt an early cache
    # lookup BEFORE the FSM/tool loop runs. On hit, stream the cached
    # answer through the same SSE delta/done format and skip the LLM run
    # entirely — the "faster on repeat questions" payoff. Strict freshness
    # guards (same data_version + similarity + TTL) live in the cache
    # service; any failure falls back to the normal run. Cached answers
    # are skipped when the turn carries file attachments (the answer
    # would depend on the file contents).
    _cache_hit_entry = None
    if (
        getattr(settings, "ECISCO_BI_RESPONSE_CACHE_ENABLED", False)
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
                    # FINALIZE is only entered after VERIFY succeeds — surface
                    # the pass + the finalize start as typed feed events.
                    if state == "finalize":
                        _push_live("verify_passed", "verify_passed")
                        _push_live("finalize_started", "finalize_started")

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

                # Persist the assistant message and emit done.
                try:
                    from app.models.chat_message import ChatMessage as Message
                    if chat_session_id:
                        msg = Message(
                            id=str(uuid4()),
                            conversation_id=conversation_id,
                            session_id=chat_session_id,
                            role="assistant",
                            content=assistant_content,
                            agent_name=_v3_agent_name,
                            trace=fsm_result.plan_summary or {},
                            reasoning=None,
                            live_events=live_events or None,
                        )
                        db.add(msg)
                        db.commit()
         