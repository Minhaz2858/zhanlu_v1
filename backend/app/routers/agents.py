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

# P4: metrics + provider fallback
from app.services.agent_metrics import metrics
from app.services.provider_fallback import with_fallback

# P4: user-level LLM overrides (temperature, max_tokens, fallback model)
from app.services.user_settings_runtime import get_user_llm_overrides

# Import tool_handlers to trigger registration of all new tools in the registry
import app.services.tool_handlers  # noqa: F401

# Synexia report pipeline — synthesize_report enriches ask_data_agent results
# with a rich ReportCardPayload (KPIs, chart, insights, actions).
from app.services.synexia.report_synthesis import synthesize_report  # noqa: E402
from app.services.synexia.finalize import finalize_into_artifact, build_no_data_payload  # noqa: E402

# Planning layer: should_trigger_planning classifies the user message and
# SynexiaFSM runs the cognitive loop (GOAL→PLAN→GATE→ACT→OBSERVE→VERIFY).
# Gated by is_fsm_enabled() (settings.SYNEXIA_FSM_ENABLED, default False).
from app.services.planning_trigger import should_trigger_planning, is_followup_refinement, PlanTrigger  # noqa: E402
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
_EMPTY_CONTENT_FALLBACK = (
    "I've completed the requested changes. "
    "Please review the agent configuration above."
)

_GENERIC_EMPTY_CONTENT_FALLBACK = (
    "I gathered some information but had trouble putting it all together. "
    "Could you try again with a more specific request?"
)

_DASHBOARD_REDIRECT_FALLBACK = (
    "I gathered some information but had trouble putting it all together. "
    "Could you try again with a more specific request? "
    "If you asked me to build a dashboard, please say 'create dashboard' and I'll try again."
)


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
    """
    if len(titles) == 1:
        return (
            f"I've completed your request. Here's the artifact: **{titles[0]}**."
        )
    joined = ", ".join(f"**{t}**" for t in titles)
    return f"I've completed your request. Here are the artifacts: {joined}."


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
    # Check if any data-agent report cards were produced
    has_report_card = False
    for tc in (tool_calls_for_frontend or []):
        result = tc.get("results") or {}
        if isinstance(result, dict) and result.get("report_card_payload"):
            has_report_card = True
            break
    if has_report_card and titles:
        return _data_summary_fallback(titles, user_content)
    if titles:
        return _artifact_aware_fallback(titles)
    if _is_dashboard_request(user_content):
        return _DASHBOARD_REDIRECT_FALLBACK
    return _GENERIC_EMPTY_CONTENT_FALLBACK


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
    low = user_content.lower()
    return any(kw in low for kw in ("dashboard", "看板", "仪表盘", "数据面板"))


def _tool_was_called(tool_calls_for_frontend: list[dict], tool_name: str) -> bool:
    """Check whether a specific tool was called during this turn."""
    return any(tc.get("name") == tool_name for tc in tool_calls_for_frontend)


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

# Per-tool overrides for the loop guard. ``memory`` is idempotent-write —
# one identical call is already a loop. ``ask_data_agent`` is capped per turn
# by name. For report creation, repeated serial data-agent calls are the
# dominant latency source; one comprehensive query should gather the full
# dataset, and a second call is the hard stop.
TOOL_CALL_CAPS: dict[str, int] = {
    "memory": 1,
    "ask_data_agent": 2,
    # Defense-in-depth against the interrupt leak: if an agent re-enables the
    # interrupt tool via tool_config, cap it at 2 calls/turn so it can never
    # eat the budget the way `interrupt(action=check)` polling did.
    "interrupt": 2,
    # clarify is a UX handoff tool; repeated calls signal a stuck agent.
    "clarify": 3,
}
_NAME_ONLY_KEYED_TOOLS: set[str] = {"ask_data_agent"}

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


def _detect_tool_call_loop(
    llm_messages: list[dict],
    start_idx: int = 0,
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
        cap = TOOL_CALL_CAPS.get(name, TOOL_CALL_HARD_CAP)
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

def _emit_activity_step(step_num: int, description: str, status: str,
                        tool_name: str | None = None,
                        detail: str | None = None,
                        command: str | None = None,
                        output_preview: str | None = None,
                        artifact_id: str | None = None,
                        duration_ms: int | None = None) -> str:
    """Return an SSE ``activity_step`` payload as a formatted SSE string.

    The optional ``command`` / ``output_preview`` fields power the
    Claude-style expandable step detail in the frontend: clicking a step
    reveals WHAT the tool was invoked with (command/code/query) and a
    short preview of what came back (output/error).

    ``duration_ms`` is the wall-clock time in milliseconds for this step,
    from the start of the LLM call or tool execution to the point where
    the step result is emitted. It is rendered in the frontend as a
    small "X.Xs" label next to the step status icon.
    """
    step = {
        "number": step_num,
        "description": description,
        "status": status,
    }
    if tool_name:
        step["tool_name"] = tool_name
    if detail:
        step["detail"] = detail
    if command:
        step["command"] = command
    if output_preview:
        step["output_preview"] = output_preview
    if artifact_id:
        step["artifact_id"] = artifact_id
    if duration_ms is not None:
        step["duration_ms"] = duration_ms
    return f'data: {json.dumps({"type": "activity_step", "step": step})}\n\n'


# ── Claude-style phase headlines ──────────────────────────────────────
# Each phase of a turn gets a short verb + descriptive clause, rendered
# as a spinner-with-headline at the top of the activity section (the
# "✳ Fathoming…" / "✳ Fabricating…" pattern). Verb vocabulary is
# deliberately evocative-but-honest: it names the moment, not magic.
PHASE_HEADLINES: dict[str, tuple[str, str]] = {
    "init":     ("Fathoming",     "Reading your request"),
    "goal":     ("Fathoming",     "Understanding what you need"),
    "context":  ("Orienting",     "Gathering the right context"),
    "plan":     ("Orchestrating", "Laying out the plan"),
    "gate":     ("Checking",      "Running policy checks"),
    "act":      ("Fabricating",   "Building your deliverable"),
    "observe":  ("Watching",      "Recording the results"),
    "verify":   ("Validating",    "Checking the outputs"),
    "finalize": ("Crystallizing", "Wrapping everything up"),
    "done":     ("Done",          "All finished"),
    "fail":     ("Stopped",       "Something went wrong"),
}


def _emit_phase(state: str, detail: str | None = None) -> str:
    """Return an SSE ``phase`` payload — the current Claude-style headline.

    Additive SSE event type; existing consumers filter on ``event.type``
    and ignore unknown types, so this is backward-compatible. The
    frontend renders the latest phase as a headline above the activity
    steps (spinner + verb + title).

    Args:
        state: FSM state name or ReAct milestone (``"goal"`` at turn
            start, ``"act"`` on the first tool batch, ``"finalize"``
            when composing the response).
        detail: Optional override for the descriptive clause.
    """
    verb, title = PHASE_HEADLINES.get(state, ("Working", state.replace("_", " ").title()))
    payload: dict = {"type": "phase", "state": state, "verb": verb, "title": detail or title}
    return f'data: {json.dumps(payload)}\n\n'


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
from typing import NamedTuple


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
    if iteration >= final_iteration and not dashboard_forced:
        return "none"
    return tool_choice


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
                        '`Skill {command: "execute <name>"}). Call '
                        "`load_skill_body(name)` to read a skill's full "
                        "methodology before using it.\n"
                        f"{priority_hint}\n<available_skills>\n"
                        f"{catalog_block}\n</available_skills>"
                    )
            except Exception:
                pass  # Catalog injection is best-effort

        # Get tool definitions (uses registry if tool_config is set)
        tools = get_tools(agent_name, tool_config, agent_app)
        # Pin web_search to the front of the tool list when the user
        # message is time-sensitive (hard-MUST grounding enforcement).
        if user_content:
            tools = apply_grounding_to_schemas(tools, user_content)

        # Auto-inject DB tools + "Bound Data Sources" prompt section if the
        # agent has data sources. Idempotent — no-op when nothing is bound.
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
                _ctx_engine = get_context_engine(_engine_name, model=get_model())
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
            model_name = get_model()
            state = _compaction_states.get(conversation_id)
            if state is None:
                state = AutoCompactState()
                _compaction_states[conversation_id] = state

            if should_autocompact(llm_messages, model_name, state):
                logger.info("Auto-compacting conversation %s (token estimate high)", conversation_id)
                llm_messages, was_compacted = await auto_compact_if_needed(
                    llm_messages,
                    model=model_name,
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
            if (
                "create_dashboard" in _tool_names
                and should_force_create_dashboard(
                    user_content,
                    tool_calls_for_frontend,
                    has_dashboard_tool=True,
                    is_dashboard_project=_is_dash_project,
                )
            ):
                logger.info(
                    "v2 stream: forcing create_dashboard after schema/design "
                    "pass (conv=%s, iter=%d, prior_tool_count=%d, dash_project=%s)",
                    conversation_id, iteration, len(tool_calls_for_frontend),
                    _is_dash_project,
                )
                tool_choice = {
                    "type": "function",
                    "function": {"name": "create_dashboard"},
                }
                dashboard_forced = True
            # Finish line: on the final iteration force tool_choice="none" so
            # the LLM must answer in text. Dashboard-guard forcing wins.
            tool_choice = _finish_line_tool_choice(
                iteration, MAX_TOOL_ITERATIONS - 1, dashboard_forced, tool_choice,
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
                if (
                    "create_dashboard" in _dash_guard_names
                    or "create_dashboard" in _v2_tool_names
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
            if len(parsed_calls) == 1:
                call = parsed_calls[0]
                _gd = guard_ctrl.before_call(call["tool_name"], call["args"])
                if _gd.allows_execution:
                    results = [await execute_tool(
                        call["tool_name"], call["args"], db,
                        user.id if user else None, context=ctx,
                    )]
                else:
                    results = [json.loads(_guardrail_synthetic_result(_gd))]
            else:
                async def _exec_one(call):
                    _gd = guard_ctrl.before_call(call["tool_name"], call["args"])
                    if _gd.allows_execution:
                        return await execute_tool(
                            call["tool_name"], call["args"], db,
                            user.id if user else None, context=ctx,
                        )
                    return json.loads(_guardrail_synthetic_result(_gd))
                raw_results = await asyncio.gather(
                    *[_exec_one(c) for c in parsed_calls],
                    return_exceptions=True,
                )
                # Convert exceptions to error dicts so processing is uniform
                results = []
                for i, r in enumerate(raw_results):
                    if isinstance(r, Exception):
                        logger.warning("Tool '%s' raised exception: %s", parsed_calls[i]["tool_name"], r)
                        results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
                    else:
                        results.append(r)

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

                        synth_result = await synthesize_report(
                            user_message=user_content,
                            rows=result.get("rows"),
                            sql=result.get("sql"),
                            source_name=result.get("source_name"),
                            source_id=result.get("source_id"),
                            call_llm_fn=_synth_call,
                        )
                        if synth_result.report_card_payload is not None:
                            # ── FINALIZE: persist Artifact row + HTML blob ──
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
                            result["report_card_payload"] = synth_result.report_card_payload.model_dump()
                            result["synthesis_text"] = synth_result.assistant_content

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
        tool_choice = _compute_tool_choice(
            user_content, data_ctx_extras, iteration,
            tool_names=_tool_names_from_schemas(tools),
        )
        # Finish line: force tool_choice="none" on the final iteration so the
        # LLM must answer in text. No dashboard guard in this loop.
        tool_choice = _finish_line_tool_choice(
            iteration, _final_iteration, False, tool_choice,
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

        if len(parsed_calls) == 1:
            call = parsed_calls[0]
            _gd = guard_ctrl.before_call(call["tool_name"], call["args"])
            if _gd.allows_execution:
                results = [await execute_tool(call["tool_name"], call["args"], db, user.id if user else None, context=ctx)]
            else:
                results = [json.loads(_guardrail_synthetic_result(_gd))]
        else:
            async def _exec_one_resume(call):
                _gd = guard_ctrl.before_call(call["tool_name"], call["args"])
                if _gd.allows_execution:
                    return await execute_tool(call["tool_name"], call["args"], db, user.id if user else None, context=ctx)
                return json.loads(_guardrail_synthetic_result(_gd))
            raw_results = await asyncio.gather(*[_exec_one_resume(c) for c in parsed_calls], return_exceptions=True)
            results = [{"success": False, "error": f"{type(r).__name__}: {r}"} if isinstance(r, Exception) else r for r in raw_results]

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
    }
    if not model_has_fixed_temperature(_model):
        payload["temperature"] = 0.7
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(_url, headers=_headers, json=payload)
            resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            status_code=e.response.status_code,
            detail=f"LLM API error: {e.response.text}",
        )
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {str(e)}")

    data = resp.json()
    choice = data["choices"][0]
    message = choice.get("message", {})
    return {"content": message.get("content", "") or ""}


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
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    if tools:
        payload["tools"] = tools
    if tool_choice:
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
    if not model_has_fixed_temperature(_model):
        base_payload["temperature"] = temperature if temperature is not None else 0.7
    if max_tokens is not None:
        base_payload["max_tokens"] = max_tokens
    if tools:
        base_payload["tools"] = tools
    if tool_choice:
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
        for m in messages
    ]

    full_content = ""
    reasoning_acc = ""
    tc_buffer: dict[int, dict] = {}

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
        nonlocal full_content, reasoning_acc
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
                if not tool_choice:
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
                    if not tool_choice:
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
                    _, _, _v3_data_ctx_extras = prepare_data_source_runtime(
                        _dsr_db,
                        _v3_agent_for_dsr,
                        [],
                        "",
                        selected_project_id=_v3_effective_pid,
                        selected_project_name=_v3_effective_pname,
                        user_id=user.id if user else None,
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

                def _on_state(state: str) -> None:
                    collected.append(_emit_fsm_state(state))
                    collected.append(_emit_phase(state))

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
                        "id": str(uuid4()),
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
                    }
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
                        db,
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
                }
                yield f'data: {json.dumps(done_payload)}\n\n'

            return StreamingResponse(
                _sse_with_heartbeat(_fsm_event_stream()),
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
    if file_urls:
        try:
            from app.services.document_ingestion.service import prepare_for_context
            _attach_parts: list[str] = []
            _image_urls: list[str] = []
            for _furl in file_urls:
                _prep = prepare_for_context(_furl)
                _fname = _prep.get("file_name") or _furl
                if _prep.get("is_image"):
                    _image_urls.append(_furl)
                    if _prep.get("text"):
                        _attach_parts.append(
                            f"--- {_fname} (image, OCR) ---\n{_prep['text']}"
                        )
                    else:
                        _attach_parts.append(
                            f"--- {_fname} (image, no OCR text — see multimodal block) ---"
                        )
                elif _prep.get("text"):
                    _attach_parts.append(
                        f"--- {_fname} ---\n{_prep['text']}"
                    )
                elif _prep.get("error"):
                    _attach_parts.append(
                        f"--- {_fname} [could not read: {_prep['error']}] ---"
                    )
            if _attach_parts:
                system_prompt += (
                    "\n\n=== User attachments ===\n"
                    + "\n\n".join(_attach_parts)
                    + "\n\nThe user uploaded the file(s) above. Answer the "
                    "user's question using their content. Quote the file "
                    "name when citing a passage.\n"
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

    # Full skill catalog injection (dynamic discovery) — see the main
    # chat path above for the full rationale.  Best-effort, token-budgeted.
    if agent_app:
        try:
            from app.services.skills_loader import list_skills as loader_list_skills
            from app.services.skills_loader import unified_search
            from app.services.skill_routing.catalog import build_catalog
            from app.services.skill_routing.post_router_hook import (
                build_agent_forced_skill_directive,
                post_router_pick,
            )

            all_skills = [s.to_dict() for s in loader_list_skills()]
            catalog_block = build_catalog(all_skills)
            if catalog_block:
                # Post-router hook: when a strong skill match fires, append a
                # HARD directive to the system prompt telling the LLM to
                # invoke the matched skill as its first action via the Skill
                # meta-tool.  Reuses unified_search results when available.
                forced_skill_directive = ""
                if user_content:
                    try:
                        results = unified_search(
                            user_content, limit=5, db=db,
                        )
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
                    except Exception:
                        pass  # Forced-skill directive is best-effort
                system_prompt += forced_skill_directive
                system_prompt += (
                    "\n\n## Skill Catalog (dynamic discovery)\n"
                    "You have access to ALL skills below — not just the "
                    "ones bound to you. Use the `Skill` tool to load or "
                    "execute any skill by name (e.g. "
                    '`Skill {command: "execute <name>"}). Call '
                    "`load_skill_body(name)` to read a skill's full "
                    "methodology before using it.\n\n"
                    f"<available_skills>\n{catalog_block}\n</available_skills>"
                )
        except Exception:
            pass

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
        tools, system_prompt, _ = prepare_data_source_runtime(
            db, agent_app, tools, system_prompt,
            selected_project_id=_v3_effective_pid,
            selected_project_name=_v3_effective_pname,
            user_id=user.id if user else None,
        )
    except Exception as e:
        logger.debug("Data source runtime prep failed (non-fatal): %s", e)
    data_ctx_extras = _v3_data_ctx_extras or {}

    llm_messages = [{"role": "system", "content": system_prompt}]
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant" and msg.get("tool_calls"):
            openai_tcs = _to_openai_tool_calls(msg["tool_calls"])
            if openai_tcs:
                llm_messages.append({
                    "role": "assistant",
                    "content": content,
                    "tool_calls": openai_tcs,
                })
            # The persistence layer saves tool results inline inside each
            # tool_call object (in the "results" key) rather than as
            # separate "role":"tool" messages.  When re-loading history we
            # must reconstruct those tool messages so the LLM sees the
            # expected pattern: assistant(tool_calls) → tool → ...
            for tc in (msg.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                tc_id = tc.get("id")
                tc_results = tc.get("results")
                if tc_id and tc_results is not None:
                    _content = tc_results if isinstance(tc_results, str) else json.dumps(tc_results)
                    llm_messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": _content,
                    })
        elif role == "tool":
            llm_messages.append({
                "role": "tool",
                "tool_call_id": msg.get("tool_call_id", ""),
                "content": msg.get("content", ""),
            })
        elif role and content is not None:
            llm_messages.append({"role": role, "content": content})

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
    # overflow (see the stream error handler).
    _stream_compaction_attempted = False
    assistant_content = ""

    async def event_stream():
        nonlocal assistant_content, guardrail_retries, messages, conv

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

        # ── Activity steps tracking (Claude-style inline numbered steps) ──
        _step_counter = [0]  # mutable counter (list for nonlocal access)
        _activity_steps: list[dict] = []  # accumulate for final persistence
        _emitted_phases: set[str] = {"goal"}  # phase headlines already sent this turn
        reasoning_acc: list[str] = []  # P0: accumulate reasoning across iterations for final assistant_msg

        # Claude-style phase headline: the turn always opens in "Fathoming".
        yield _emit_phase("goal")

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
        _schema_edges_seen = False
        for iteration in range(MAX_TOOL_ITERATIONS):
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
            loop_info = _detect_tool_call_loop(llm_messages, start_idx=_turn_start_idx)
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
                    conversation_id, looped_tool, looped_n, TOOL_CALL_HARD_CAP,
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
            # Also fires in dashboard-capable projects (Ecisco BI) when the
            # agent speculatively loads dashboard skills before being asked.
            _tool_names = _tool_names_from_schemas(tools)
            _is_dash_project = bool(conv.agent_name and (
                "ecisco_bi" in conv.agent_name.lower()
                or "bi_assistant" in conv.agent_name.lower()
            ))
            if (
                "create_dashboard" in _tool_names
                and should_force_create_dashboard(
                    user_content,
                    tool_calls_for_frontend,
                    has_dashboard_tool=True,
                    is_dashboard_project=_is_dash_project,
                )
            ):
                logger.info(
                    "v3 stream: forcing create_dashboard after schema/design "
                    "pass (conv=%s, iter=%d, prior_tool_count=%d, dash_project=%s)",
                    conversation_id, iteration, len(tool_calls_for_frontend),
                    _is_dash_project,
                )
                tool_choice = {
                    "type": "function",
                    "function": {"name": "create_dashboard"},
                }
                dashboard_forced = True

            # Finish line: on the final iteration force tool_choice="none" so
            # the LLM must answer in text. Dashboard-guard forcing wins.
            tool_choice = _finish_line_tool_choice(
                iteration, MAX_TOOL_ITERATIONS - 1, dashboard_forced, tool_choice,
            )

            # ── Gap 1: true token streaming ───────────────────────────
            # When STREAM_TOKEN_DELTAS is on, drive the streaming generator
            # (_stream_llm_with_tools) so tokens are yielded to the client
            # as they arrive (typing effect). The generator also
            # reassembles fragmented tool_calls so agent tool execution
            # continues to work. When the flag is off, fall back to the
            # legacy buffered _call_llm_with_tools path (unchanged).
            assistant_content = ""
            raw_tool_calls: list = []
            final_reasoning = ""
            content_streamed = False  # tracks whether deltas were sent live

            try:
                # P1.3: Pre-API deterministic tool result pruning
                prune_tool_results_only(llm_messages, model=get_model())
                sanitize_messages(llm_messages)
                # Fix D: on the final text-only iteration, condense oversized
                # ask_data_agent report cards so the synthesis LLM keeps its
                # output budget (prevents empty-content synthesis).
                if tool_choice == "none":
                    _condense_data_agent_results(llm_messages)
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
                                    yield f'data: {json.dumps({"type": "delta", "content": ev_data})}\n\n'
                                elif ev_type == "reasoning":
                                    final_reasoning += ev_data
                                elif ev_type == "tool_calls":
                                    raw_tool_calls = ev_data or []
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
                    and not _stream_compaction_attempted
                ):
                    # Context overflow: one reactive compaction pass, then
                    # re-enter the iteration with compacted messages.
                    _stream_compaction_attempted = True
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
                # No guardrail trigger — stream the final response
                # P2.1: Verification-on-stop
                _verify_nudge = build_verify_on_stop_nudge(llm_messages, attempts=_verify_attempts)
                if _verify_nudge:
                    llm_messages.append({"role": "assistant", "content": assistant_content})
                    llm_messages.append({"role": "user", "content": _verify_nudge})
                    _verify_attempts += 1
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

            # ── Dashboard guard interception ──────────────────────────
            # The guard above set tool_choice to force create_dashboard, but
            # weaker models (e.g. qwen3.5-27b) often ignore that and return
            # execute_query instead. Block those calls AND inject a synthetic
            # tool result so the LLM sees why its call was rejected and
            # retries with create_dashboard. Only `create_dashboard` and
            # harmless side-effect tools are allowed through.
            if parsed_calls:
                _dash_guard_names = {p["tool_name"] for p in parsed_calls}
                if (
                    "create_dashboard" in _dash_guard_names
                    or "create_dashboard" in _tool_names
                ) and any(
                    n in dashboard_guard_blocked_tools()
                    for n in _dash_guard_names
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

            _tool_step_indices: list[int] = []
            # Count parallel calls per tool for index suffix
            _tool_counts: dict[str, int] = {}
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
            }

            if len(parsed_calls) == 1:
                call = parsed_calls[0]
                _gd = guard_ctrl.before_call(call["tool_name"], call["args"])
                if _gd.allows_execution:
                    results = [await execute_tool(
                        call["tool_name"], call["args"],
                        db, user.id if user else None, context=ctx,
                    )]
                else:
                    results = [json.loads(_guardrail_synthetic_result(_gd))]
            else:
                async def _exec_one(call):
                    _gd = guard_ctrl.before_call(call["tool_name"], call["args"])
                    if _gd.allows_execution:
                        return await execute_tool(
                            call["tool_name"], call["args"], db,
                            user.id if user else None, context=ctx,
                        )
                    return json.loads(_guardrail_synthetic_result(_gd))
                raw_results = await asyncio.gather(
                    *[_exec_one(c) for c in parsed_calls],
                    return_exceptions=True,
                )
                results = []
                for i, r in enumerate(raw_results):
                    if isinstance(r, Exception):
                        logger.warning("Tool '%s' raised: %s", parsed_calls[i]["tool_name"], r)
                        results.append({"success": False, "error": f"{type(r).__name__}: {r}"})
                    else:
                        results.append(r)

            # Process results — check for approval pause
            paused_for_approval = False
            pending_tool = None
            for idx, (call, result) in enumerate(zip(parsed_calls, results)):
                tool_name = call["tool_name"]
                args_str = call["args_str"]
                tool_call_id = call["tool_call_id"]
                display_name = TOOL_DISPLAY_NAMES.get(tool_name, tool_name)

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

                # ── Report synthesis: enrich ask_data_agent results ──────
                if tool_name == "ask_data_agent" and isinstance(result, dict) and result.get("rows"):
                    try:
                        _synth_endpoint = effective_llm.endpoint

                        async def _synth_call(system_prompt, msgs, _ep=_synth_endpoint):
                            return await _call_synthesis_llm(
                                system_prompt, msgs, endpoint=_ep
                            )

                        synth_result = await synthesize_report(
                            user_message=user_content,
                            rows=result.get("rows"),
                            sql=result.get("sql"),
                            source_name=result.get("source_name"),
                            source_id=result.get("source_id"),
                            call_llm_fn=_synth_call,
                        )
                        if synth_result.report_card_payload is not None:
                            # ── FINALIZE: persist Artifact row + HTML blob ──
                            # NOTE: finalize_into_artifact may mutate
                            # `payload.user_signal` in-place when a
                            # file-format intent is detected (e.g. the
                            # user asked for DOCX).  We MUST re-serialize
                            # the payload AFTER finalize so the mutated
                            # field reaches the frontend.
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
                            # Re-serialize AFTER finalize so user_signal
                            # reflects any change (e.g. "export_docx").
                            result["report_card_payload"] = synth_result.report_card_payload.model_dump()
                            result["synthesis_text"] = synth_result.assistant_content

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
                            "v3 FINALIZE (no-data): artifact_id=%s attached for conv=%s (doc_format=%s)",
                            getattr(artifact, 'id', None) if artifact else None,
                            conversation_id, _orch_doc_format,
                        )
                    except Exception as no_data_err:
                        logger.warning(
                            "v3 No-data finalize failed (non-fatal) for conv=%s: %s",
                            conversation_id, no_data_err,
                        )

                tool_call_record = {
                    "id": tool_call_id, "name": display_name,
                    "arguments_string": args_str, "results": result,
                    "status": "completed" if result.get("success") else "failed",
                }
                if tool_name in INTERNAL_TOOLS:
                    tool_call_record["display_projection"] = _internal_tool_projection(tool_name)
                tool_calls_for_frontend.append(tool_call_record)

                # ── Dynamic budget upgrade ───────────────────────────
                # describe_schema revealed schema-graph join edges: widen
                # the per-turn soft cap so the agent can explore the joins
                # instead of being nudged to wrap up too early. Idempotent
                # (runs once, on the first edge-bearing result).
                if not _schema_edges_seen and tool_name == "describe_schema":
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
                    yield _emit_activity_step(
                        step_num, step_desc, new_status, tool_name=tool_name,
                        command=_step_cmd, output_preview=_step_out,
                        artifact_id=_step_art,
                        duration_ms=int((time.monotonic() - _step_start_times.get(step_num, time.monotonic())) * 1000),
                    )

                llm_messages.append({
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": tool_call_id, "type": "function",
                                    "function": {"name": tool_name, "arguments": args_str}}],
                })
                # P0: apply Layer 2 (per-result) persistence + guardrail after_call
                _result_str = _persisted_result_str(tool_name, result, conversation_id)
                llm_messages.append({
                    "role": "tool", "tool_call_id": tool_call_id,
                    "content": _result_str,
                })
                guard_ctrl.after_call(tool_name, call["args"], _result_str)
                # P8: Record learning
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
        # BUGFIX: skip fallback if deltas were already streamed — otherwise
        # the user sees "detailed report" + "I gathered some information..."
        # as a confusing double-message.
        if not assistant_content and not locals().get("content_streamed"):
            assistant_content = _EMPTY_CONTENT_FALLBACK if agent_name == "agent_builder" else _choose_fallback(
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

        # ── Activity step: "Generating response" done ──
        _step_counter[0] += 1
        _gen_dur = int((time.monotonic() - _step_start_times.get(_step_counter[0] - 1, time.monotonic())) * 1000)
        yield _emit_activity_step(
            _step_counter[0], "Generating response", "done",
            duration_ms=_gen_dur,
        )
        _activity_steps.append({
            "number": _step_counter[0],
            "description": "Generating response",
            "status": "done",
            "duration_ms": _gen_dur,
        })

        # ── Marker contract: ◤MD_DOCX◤ / ◤HTML_DOCX◤ / ◤PPTX◤ ──
        # Strip markers from the streamed + persisted assistant content, and
        # route each marker through the generation orchestrator (which properly
        # *awaits* the async _create_artifact_tool — the previous synchronous
        # call produced a never-awaited coroutine, so markers never yielded
        # artifacts). Also runs the server-driven doc-request fallback (Q1) so
        # a confirmed file request always yields an artifact. Best-effort: a
        # marker/fallback failure must never break the SSE stream.
        _orch_created: list[dict] = []
        # (_orch_doc_format was computed before the tool loop.)
        try:
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
        if not content_streamed:
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
        if not (assistant_content or "").strip():
            try:
                # Dashboard-specific rescue: if the user asked for a
                # dashboard but create_dashboard was never called, inject
                # a final nudge forcing the LLM to call create_dashboard
                # with whatever schema info it has. This prevents the
                # common "budget exhausted during exploration" failure.
                if (
                    _is_dashboard_request(user_content)
                    and not _tool_was_called(tool_calls_for_frontend, "create_dashboard")
                    and not _tool_was_called(tool_calls_for_frontend, "update_dashboard")
                ):
                    logger.info(
                        "Dashboard rescue: user asked for dashboard but "
                        "create_dashboard never called (conv=%s). Injecting "
                        "rescue nudge.",
                        conversation_id,
                    )
                    rescue_nudge = (
                        "CRITICAL: The user asked for a dashboard but you ran "
                        "out of tool iterations before calling create_dashboard. "
                        "You MUST call create_dashboard NOW with the schema "
                        "information you already have. Use the column names you "
                        "discovered from describe_schema/execute_query. Do NOT "
                        "explore further — build the dashboard with what you know."
                    )
                    llm_messages.append({"role": "user", "content": rescue_nudge})
                    # One final LLM call to force create_dashboard
                    try:
                        _rescue_stream = _stream_llm_with_tools(
                            llm_messages, tools,
                            tool_choice={"type": "function", "function": {"name": "create_dashboard"}},
                            model_override=user_model,
                            endpoint=effective_llm.endpoint,
                            temperature=llm_overrides.get("temperature"),
                            max_tokens=llm_overrides.get("max_tokens"),
                        )
                        async for _ev_type, _ev_data in _rescue_stream:
                            if _ev_type == "delta":
                                assistant_content += _ev_data
                                content_streamed = True
                                yield f'data: {json.dumps({"type": "delta", "content": _ev_data})}\n\n'
                            elif _ev_type == "tool_calls":
                                raw_tool_calls = _ev_data or []
                        # If the rescue produced a create_dashboard call, execute it
                        if raw_tool_calls:
                            for _tc in raw_tool_calls:
                                _fn = _tc.get("function", {})
                                _tname = _fn.get("name", "")
                                if _tname == "create_dashboard":
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
                                        "name": "create_dashboard",
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
                                        "Dashboard rescue: create_dashboard executed "
                                        "(success=%s, conv=%s)",
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
                        # Dashboard-specific fallback message
                        if _is_dashboard_request(user_content) and not _tool_was_called(tool_calls_for_frontend, "create_dashboard"):
                            assistant_content = (
                                "I ran out of steps while exploring the data for your dashboard. "
                                "Please try again — I'll create the dashboard more quickly this time."
                            )
                        else:
                            # Context-aware fallback: choose the best message
                            # based on what was produced this turn (report cards,
                            # artifacts, dashboard requests, or generic).
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

        # Save the final assistant message
        assistant_msg = {
            "id": assistant_msg_id, "role": "assistant",
            "content": assistant_content,
            "created_date": datetime.now(timezone.utc).isoformat(),
        }
        if tool_calls_for_frontend:
            assistant_msg["tool_calls"] = tool_calls_for_frontend
        # Attach artifact_ids so the frontend can render ArtifactPreviewCard
        if artifact_ids:
            assistant_msg["artifact_ids"] = artifact_ids
        # Attach activity_steps so they survive page refresh
        if _activity_steps:
            assistant_msg["activity_steps"] = _activity_steps
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
        try:
            db.commit()
        except Exception as _commit_err:
            logger.error("v3 stream final commit failed: %s", _commit_err)
            db.rollback()
            raise
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
        try:
            if getattr(settings, "QUALITY_EVAL_ALL_PATHS", True):
                from app.services.synexia.quality_eval import evaluate_response_quality
                _qe_result = evaluate_response_quality(
                    user_message=user_content or "",
                    assistant_text=assistant_content,
                )
                if _qe_result.final_text and _qe_result.final_text != assistant_content:
                    assistant_content = _qe_result.final_text
                # Emit quality eval verdict as SSE event
                yield f'data: {json.dumps({"type": "quality_eval", "quality_eval": _qe_result.to_dict()})}\n\n'
        except Exception as _qe_err:
            logger.warning("v3 stream quality eval failed (non-fatal): %s", _qe_err)

        # --- Experience layer (Phase A/B): recipe + profile + cache store ---
        try:
            _record_turn_experience(
                agent_app_id=agent_app_id,
                user_id=getattr(user, "id", None),
                user_content=user_content,
                assistant_content=assistant_content,
                tool_sequence=tool_calls_for_frontend,
                iterations=len(tool_calls_for_frontend or []),
            )
            _store_turn_cache(
                db,
                agent_app_id=agent_app_id,
                user_id=getattr(user, "id", None),
                user_content=user_content,
                assistant_content=assistant_content,
                artifact_ids=assistant_msg.get("artifact_ids") if isinstance(assistant_msg, dict) else None,
            )
        except Exception as _xp_err:  # noqa: BLE001 — best-effort
            logger.warning("experience: turn-end hook failed (non-fatal): %s", _xp_err)

        yield f'data: {json.dumps({"type": "done", "content": assistant_content, "trace": assistant_msg.get("trace", []), "conversation": conv.to_dict()})}\n\n'
        _discard_steer(conversation_id)

    return StreamingResponse(
        _sse_with_heartbeat(event_stream()),
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
