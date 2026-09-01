"""ask_data_agent — delegate a database question to the builtin Data Agent.

Reuses the conversation-loop pattern from `delegate_tool` but with two
important differences:

1. The sub-agent is always `data_agent` (no `agent_name` parameter).
2. The sub-agent's toolset is exactly the 4 DB tools, scoped to the
   calling agent's bound data sources.

Return shape (consumed by the calling agent's tool result):

    {
        "success": True,
        "answer":   "<prose narrative>",        # the Data Agent's final text
        "rows":     [...],                      # rows from the last execute_query
        "sql":      "SELECT ...",               # the SQL the Data Agent ran
        "source_id":   "<kb_id>",
        "source_name": "<kb.name>",
        "iterations": N,                        # sub-agent tool-call count
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
from app.services.answer_verification import (  # universal self-eval gate
    build_gap_disclosure,
    build_replan_nudge,
    evaluate_answer,
)
from app.services.goal_contract import is_metadata_only_rows
from app.services.tool_handlers import db_tools  # ensure DB tools registered
from app.services.tool_registry import registry
from app.services.sub_agent_reliability import (
    call_llm_with_reliability,
    pre_call_prep,
    persist_result_str,
    apply_turn_budget_to_batch,
    ToolLoopGuardController,
    synthetic_blocked_result,
    IterationBudget,
    metrics,
)

logger = logging.getLogger(__name__)


# Hard wall-clock budget for a single data-agent delegation (env-tunable).
# The sub-agent loop is capped at this many seconds regardless of how many
# iterations remain — a slow warehouse query or a rambling sub-model must
# never hold the user's turn hostage. On timeout we return whatever rows
# were captured so far (with `truncated: True`).
#
# 2026-08-25: lowered from 90s → 60s per user request. A slow query must not
# kill the SSE stream — partial rows + `truncated=True` is preferable to the
# frontend's "Sorry, I hit an error" fallback firing after the data table
# already rendered. The v3 stream loop's smart-retry path picks up from the
# partial rows and either re-plans (Hermes) or synthesizes what's available.
DATA_AGENT_BUDGET_SECONDS = float(
    os.environ.get("DATA_AGENT_BUDGET_SECONDS", "60")
)


# Tools the Data Agent sub-loop is allowed to use.
_DATA_AGENT_TOOLS = [
    "list_data_sources",
    "describe_schema",
    "execute_query",
    "answer_from_database",
    "search_documents",
    "answer_from_documents",
]

# Tools it is NOT allowed to call (prevent loops and writes).
_DATA_AGENT_DENIED = {
    "ask_data_agent", "fetch_data_batch", "delegate_task", "memory", "send_message",
    "create_agent", "update_agent", "create_skill", "update_skill",
    "create_automation", "update_automation",
}

# ── Trivial-probe / garbage-data detection ──────────────────────────────────
# Patterns that indicate a schema probe rather than a real business query.
_TRIVIAL_PROBE_RE = re.compile(
    r"\bselect\s+\*\s+from\s+\S+\s+limit\s+(1|5|10)\b",
    re.IGNORECASE,
)

# Internal ID columns that should NEVER appear as KPI/chart values.
# Pattern: F-prefixed uppercase ending in ID (common in ERP systems), or
# common generic ID names. Detected dynamically by _is_internal_id_column().
_INTERNAL_ID_PATTERNS = frozenset({
    "id", "ID", "rowid", "ROWID", "uid", "UID", "uuid",
    "pk", "PK",
})

# Business-meaningful column name fragments (case-insensitive match).
# Generic terms found in business databases across industries.
_BUSINESS_COL_FRAGMENTS = frozenset({
    "amount", "revenue", "quantity", "qty", "price", "cost", "profit",
    "margin", "name", "product", "material", "customer", "region",
    "date", "total", "sum", "count", "volume", "weight", "unit",
    "tax", "discount", "subtotal", "grand", "net", "gross",
    "sales", "order", "invoice", "payment", "receipt",
    "inventory", "stock", "warehouse", "shipment", "delivery",
})


def _is_trivial_probe(sql: str | None) -> bool:
    """Return True if the SQL looks like a schema probe (LIMIT N without
    aggregation, WHERE, or GROUP BY) rather than a real business query."""
    if not sql:
        return False
    s = sql.strip().lower()
    # SELECT * FROM table LIMIT 1/5/10 without WHERE or GROUP BY
    if _TRIVIAL_PROBE_RE.search(s):
        if "where" not in s and "group by" not in s:
            return True
    # Any SELECT * without aggregation on a large table
    if re.search(r"\bselect\s+\*\s+from\b", s) and "group by" not in s and "where" not in s and "limit" not in s:
        return True
    return False


def _is_garbage_business_data(rows: list | None) -> bool:
    """Return True when the rows contain ONLY internal ID columns and no
    business-meaningful columns. This catches the case where the sub-agent
    ran `SELECT * FROM table LIMIT 1` and got back FENTRYID/FID garbage."""
    if not rows:
        return False  # empty is not garbage, just empty
    if not isinstance(rows, list) or len(rows) == 0:
        return False
    first = rows[0] if isinstance(rows[0], dict) else {}
    if not first:
        return False
    cols = set(first.keys())
    # If ALL columns are internal IDs, it's garbage
    # Use _is_internal_id_column() for dynamic F-prefix + _id detection
    if cols and all(
        c in _INTERNAL_ID_PATTERNS or
        (len(c) > 2 and c[0] == "F" and c[1].isupper() and c.endswith("ID")) or
        c.endswith("_id") or c.endswith("_ID")
        for c in cols
    ):
        return True
    # If no column contains any business fragment, it's likely garbage
    has_business = any(
        any(frag in col.lower() for frag in _BUSINESS_COL_FRAGMENTS)
        for col in cols
    )
    return not has_business


def _rows_have_business_data(rows: list | None) -> bool:
    """Return True when rows contain at least one business-meaningful column
    AND at least one row has non-null values in business columns."""
    if not rows or not isinstance(rows, list) or len(rows) == 0:
        return False
    first = rows[0] if isinstance(rows[0], dict) else {}
    if not first:
        return False
    cols = set(first.keys())
    business_cols = {
        c for c in cols
        if any(frag in c.lower() for frag in _BUSINESS_COL_FRAGMENTS)
    }
    if not business_cols:
        return False
    # Check at least one row has a non-null business value
    for row in rows[:20]:
        if isinstance(row, dict):
            for bc in business_cols:
                if row.get(bc) is not None:
                    return True
    return False


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

async def _ask_data_agent(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}

    preferred_kb = args.get("data_source_id")
    bound_kb_ids: list[str] = list((context or {}).get("bound_kb_ids") or [])
    endpoint = (context or {}).get("endpoint")

    # ── Resource router (flag-gated): a confident non-database route
    # short-circuits the data sub-agent so the caller picks the right
    # resource instead of burning 3-5 LLM calls in the loop. Fallback
    # decisions (used_fallback=True) are ignored — behave as today.
    #
    # DOCUMENT routes are NOT short-circuited: the data sub-agent ships
    # document tools (search_documents / answer_from_documents) and its
    # prompt teaches them, so a question routed to a bound file-KB must
    # flow through the loop below. Only MEMORY / REPORT (resources the
    # data agent genuinely cannot serve) short-circuit.
    if getattr(settings, "KG_RESOURCE_ROUTER_ENABLED", False):
        try:
            from app.services.knowledge_graph.resource_router import (
                ResourceRoute,
                available_resources_from_context,
                route_question,
            )

            _avail = available_resources_from_context(context or {}, db)
            _decision = route_question(question, available_resources=_avail)
            if (
                not _decision.used_fallback
                and _decision.route
                not in (
                    ResourceRoute.DATABASE,
                    ResourceRoute.DOCUMENT,
                    ResourceRoute.MULTI_RESOURCE,
                )
            ):
                return {
                    "success": False,
                    "error_kind": "wrong_resource_route",
                    "route": _decision.route.value,
                    "error": (
                        f"This question was routed to '{_decision.route.value}' "
                        "resources, not a database. Use the appropriate "
                        "document/memory/report path instead of the Data Agent."
                    ),
                }
        except Exception as e:
            logger.debug("ask_data_agent: resource router skipped (non-fatal): %s", e)

    if preferred_kb and bound_kb_ids and preferred_kb not in bound_kb_ids:
        return {
            "success": False,
            "error": (
                f"KnowledgeBase {preferred_kb!r} is not bound to this agent. "
                f"Bound data sources: {bound_kb_ids}"
            ),
        }

    # ── Fast path (flag-gated): a single bound database source goes
    # through the one-shot NL2SQL pipeline (2 LLM calls) instead of the
    # iterative sub-agent loop (3-5 calls). Returns None on any failure
    # so the loop below runs as fallback.
    if settings.DATA_AGENT_FASTPATH_ENABLED:
        fastpath_result = await _try_nl_fastpath(
            question, preferred_kb, bound_kb_ids, db, endpoint=endpoint,
            skip_narrate=bool((context or {}).get("main_agent_will_synthesize")),
            agent_name=(context or {}).get("agent_name") or (context or {}).get("caller"),
        )
        if fastpath_result is not None:
            return fastpath_result

    # ── Opt-in progress sub-step emitter (wired by the v3 FSM for chat
    # streaming). When the FSM passes ``context["progress_emitter"]`` (a
    # mutable list) + ``context["step_counter"]`` (the FSM's ``[int]``
    # counter), we emit 3 coarse sub-steps so the UI shows what the Data
    # Agent is doing instead of one long "Querying the bound data source"
    # block. It is a no-op when the FSM doesn't provide the emitter, so it
    # is safe for every other caller (tests, batch jobs, other routers).
    _progress = (context or {}).get("progress_emitter")
    _progress_step_ctr = (context or {}).get("step_counter")
    _progress_timings: dict[str, float] = {}
    _progress_done: set[str] = set()

    def _prog_begin(key: str, label: str) -> None:
        if _progress is None or _progress_step_ctr is None or key in _progress_done:
            return
        _progress_timings[key] = time.monotonic()
        _progress_step_ctr[0] += 1
        from app.services.agent_loop.sse_builders import _emit_activity_step

        _progress.append(
            _emit_activity_step(_progress_step_ctr[0], label, "running", tool_name="ask_data_agent")
        )

    def _prog_end(key: str, label: str) -> None:
        if _progress is None or _progress_step_ctr is None or key in _progress_done:
            return
        _progress_done.add(key)
        dur = None
        if key in _progress_timings:
            dur = int((time.monotonic() - _progress_timings[key]) * 1000)
        _progress_step_ctr[0] += 1
        from app.services.agent_loop.sse_builders import _emit_activity_step

        _progress.append(
            _emit_activity_step(
                _progress_step_ctr[0], label, "done", tool_name="ask_data_agent", duration_ms=dur
            )
        )

    _prog_begin("schema", "Resolving data source schema")

    system_prompt = _build_sub_agent_prompt(
        question,
        bound_kb_ids,
        preferred_kb,
        caller=(context or {}).get("agent_name"),
    )
    tool_schemas = _build_sub_agent_tool_schemas()

    # Track the most recent execute_query result for the structured return.
    state: dict = {
        "last_rows": None,
        "last_sql": None,
        "source_id": preferred_kb,
        "source_name": None,
    }

    sub_context = dict(context or {})
    sub_context["bound_kb_ids"] = bound_kb_ids
    if preferred_kb:
        sub_context["data_source_id"] = preferred_kb
    # Agent-level opt-in for the schema validator (NOT a global flag flip):
    # the Data Agent's execute_query calls get did-you-mean suggestions + FK
    # master hints as structured error context, so it self-corrects instead
    # of silently degrading after a bad query.  The validator is feedback,
    # not a gate — it never blocks valid SQL.
    sub_context["schema_validator_enabled"] = True
    sub_context["did_you_mean_enabled"] = True
    # Enable schema linker + schema graph for the Data Agent so it gets
    # join edges + FK relationships automatically.  These are agent-level
    # opt-ins (NOT global flag flips) — _describe_schema checks context
    # in addition to settings flags.
    sub_context["schema_linking_enabled"] = True
    sub_context["schema_graph_enabled"] = True

    iterations = 0
    _gate_attempts = 0  # universal self-eval re-plan nudges this sub-loop turn
    # Default 2 iterations. The Data Agent's loop is now optimized so a
    # schema-discover -> execute_query pair is the common case: the calling
    # agent embeds a compact [schema: ...] hint in the question when it can,
    # so the sub-agent skips describe_schema entirely and needs only ONE
    # iteration to run the query. A budget of 4+ encouraged the sub-model to
    # keep "refining" long past the point of usefulness (the 162.6s turns
    # users saw). The wall-clock cap (DATA_AGENT_BUDGET_SECONDS) still
    # bounds total time; the hard cap is 6 for genuinely complex multi-table
    # questions.
    max_iterations = min(args.get("max_iterations", 4), 10)
    final_text = ""
    # Per-call wall-clock override (2026-08-27): the orchestrator injects
    # `budget_seconds` for dashboard turns so data collection is NOT
    # truncated before every metric is gathered (default 60s cut the
    # delegate mid-collection on a dashboard build). Falls back to the
    # module constant.
    _budget_s = float(args.get("budget_seconds") or DATA_AGENT_BUDGET_SECONDS)
    # P0: wall-clock + per-phase timing for the delegation.
    _delegation_start = time.monotonic()
    _llm_total_s = 0.0
    _sql_total_s = 0.0
    _truncated = False
    # Distinguish two failure modes:
    #   _initial_llm_failure: set ONLY when the FIRST _call_llm in the
    #     sub-loop raises. No rows are guaranteed, so the whole call is
    #     a hard failure. Flips `success` to False.
    #   _synthesis_llm_failure: set when the optional synthesis LLM call
    #     raises AFTER rows were captured. We already have a valid
    #     answer (the rows), so this is a soft failure — we log it but
    #     keep `success: True` so the calling agent's activity step
    #     doesn't show a red "failed" indicator.
    _initial_llm_failure: Optional[str] = None
    _synthesis_llm_failure: Optional[str] = None

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    # P0 reliability: per-turn guardrail controller + iteration budget
    guard_ctrl = ToolLoopGuardController()
    conv_budget = IterationBudget(max_total=max_iterations)

    for iteration in range(max_iterations):
        # P0: consume one iteration from the conversation-level budget
        if not conv_budget.consume():
            logger.info(
                "Sub-agent (data) iteration budget exhausted (%d/%d), breaking",
                conv_budget.used, conv_budget.max_total,
            )
            break
        iterations += 1

        # P0: wall-clock budget — hard cap on the sub-agent's total time.
        # Even if iterations remain, a slow warehouse query or a rambling
        # sub-model must not hold the user's turn hostage. On truncation we
        # break out and return whatever rows were captured (see the
        # directive fallback below), tagged `truncated: True`.
        if time.monotonic() - _delegation_start > _budget_s:
            logger.warning(
                "ask_data_agent wall-clock budget exceeded (%.1fs > %.1fs), truncating",
                time.monotonic() - _delegation_start,
                _budget_s,
            )
            _truncated = True
            break

        # P1.3/P2: pre-API pruning + message sanitization
        pre_call_prep(messages)

        _llm_t0 = time.monotonic()
        try:
            llm_response = await _call_llm_with_retry(messages, tool_schemas, endpoint=endpoint)
        except Exception as e:
            logger.warning("ask_data_agent LLM call failed on iter %d: %s", iteration, e)
            # First-iteration failure is a HARD failure: no rows
            # guaranteed, and the calling LLM has no signal beyond
            # whatever we put in `final_text`. Later-iteration
            # failures may still have rows from a previous iteration;
            # we set initial_llm_failure and fall through to the
            # synthesis / fallback path.
            _initial_llm_failure = str(e)
            break
        _llm_total_s += time.monotonic() - _llm_t0

        # First LLM call resolved the schema/plan → hand off to SQL generation.
        if iteration == 0:
            _prog_end("schema", "Resolving data source schema")
            _prog_begin("sql", "Generating SQL query")

        content = llm_response.get("content", "") or ""
        raw_tool_calls = llm_response.get("tool_calls", []) or []

        if not raw_tool_calls:
            final_text = content
            # P2.2: Universal Self-Evaluation gate (sub-loop) — verify the
            # draft answer against the question before accepting it. On
            # INCOMPLETE (e.g. metadata-only answer), inject the gap nudge
            # and continue so the agent re-plans with a real data query.
            _g_action, _g_msg = await _sub_loop_answer_gate(
                question,
                state,
                final_text,
                attempts=_gate_attempts,
                budget_remaining=max_iterations - iteration,
                endpoint=endpoint,
            )
            if _g_action == "nudge":
                messages.append({"role": "assistant", "content": final_text})
                messages.append({"role": "user", "content": _g_msg})
                _gate_attempts += 1
                continue
            if _g_action == "disclose":
                final_text = (final_text or "") + _g_msg
            break

        # Append assistant message
        assistant_msg = {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": tc.get("id", str(uuid.uuid4())),
                    "type": "function",
                    "function": {
                        "name": tc["function"]["name"],
                        "arguments": tc["function"]["arguments"],
                    },
                }
                for tc in raw_tool_calls
            ],
        }
        _reasoning = (llm_response.get("reasoning") or "").strip()
        if _reasoning:
            assistant_msg["reasoning_content"] = _reasoning
        messages.append(assistant_msg)

        # P0: per-tool guardrail + per-result persistence
        batch_ids: list[str] = []
        batch_names: list[str] = []

        # --- Parallel execution for independent read-only tools ---
        # When the LLM emits multiple tool calls in a single turn (e.g.
        # list_data_sources + describe_schema, or several independent
        # execute_query calls for different metrics), run them concurrently
        # via asyncio.gather instead of sequentially. This cuts 1-2 turns of
        # latency for schema-discovery questions AND turns multi-metric
        # queries (volume + revenue + margin) into a single parallel burst.
        # Only read-only tools that don't depend on each other's results are
        # parallelized; execute_query is read-only (SELECT-only enforced by
        # the query validator).
        _PARALLEL_SAFE_TOOLS = {
            "list_data_sources",
            "describe_schema",
            "search_documents",
            "execute_query",
        }

        # Pre-parse all tool calls for this batch
        _parsed_calls: list[tuple[str, dict, str]] = []
        for tc in raw_tool_calls:
            tool_name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments", "{}")
            tool_call_id = tc.get("id", str(uuid.uuid4()))
            try:
                tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                tool_args = {}
            _parsed_calls.append((tool_name, tool_args, tool_call_id))

        # Check if ALL calls in this batch are parallel-safe and independent
        _all_parallel_safe = all(
            name in _PARALLEL_SAFE_TOOLS for name, _, _ in _parsed_calls
        ) and len(_parsed_calls) > 1

        # SQL is generated; move the progress sub-step into "executing".
        if iteration == 0:
            _prog_end("sql", "Generating SQL query")
            _prog_begin("exec", "Executing query & fetching rows")

        if _all_parallel_safe:
            # Run all tool calls concurrently
            async def _run_one(tn: str, ta: dict, db: Session, uid: str | None, ctx: dict):
                _gd = guard_ctrl.before_call(tn, ta)
                if not _gd.allows_execution:
                    return json.loads(synthetic_blocked_result(_gd))
                if tn not in _DATA_AGENT_TOOLS:
                    return {"success": False, "error": f"Tool {tn!r} is not available to the Data Agent."}
                from app.services.agent_tools import execute_tool
                return await execute_tool(tn, ta, db, uid, context=ctx)

            _coros = [_run_one(tn, ta, db, user_id, sub_context) for tn, ta, _ in _parsed_calls]
            _sql_t0 = time.monotonic()
            _results = await asyncio.gather(*_coros, return_exceptions=True)
            _sql_total_s += time.monotonic() - _sql_t0

            for (tn, ta, tcid), result in zip(_parsed_calls, _results):
                batch_ids.append(tcid)
                batch_names.append(tn)
                if isinstance(result, Exception):
                    result = {"success": False, "error": str(result)}
                _maybe_capture_execute_result(tn, result, state)
                _result_str = persist_result_str(
                    tn, result, None,
                    context_window_tokens=(
                        endpoint.context_window if endpoint else None
                    ),
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tcid,
                    "content": _result_str,
                })
                guard_ctrl.after_call(tn, ta, _result_str)
        else:
            # Sequential execution (original path)
            for tc in raw_tool_calls:
                tool_name = tc.get("function", {}).get("name", "")
                raw_args = tc.get("function", {}).get("arguments", "{}")
                tool_call_id = tc.get("id", str(uuid.uuid4()))
                try:
                    tool_args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    tool_args = {}
                batch_ids.append(tool_call_id)
                batch_names.append(tool_name)

                # P0: guardrail before_call — block looping tools
                _gd = guard_ctrl.before_call(tool_name, tool_args)
                if not _gd.allows_execution:
                    result = json.loads(synthetic_blocked_result(_gd))
                elif tool_name not in _DATA_AGENT_TOOLS:
                    result = {
                        "success": False,
                        "error": f"Tool {tool_name!r} is not available to the Data Agent.",
                    }
                else:
                    # Route through the same execute_tool path the runtime uses
                    # — so permissions, hooks, and logging all work uniformly.
                    from app.services.agent_tools import execute_tool
                    _sql_t0 = time.monotonic()
                    result = await execute_tool(
                        tool_name, tool_args, db, user_id, context=sub_context
                    )
                    _sql_total_s += time.monotonic() - _sql_t0
                    _maybe_capture_execute_result(tool_name, result, state)

                # P0: Layer 2 per-result persistence (large results → disk preview)
                _result_str = persist_result_str(
                    tool_name, result, None,
                    context_window_tokens=(
                        endpoint.context_window if endpoint else None
                    ),
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": _result_str,
                })
                # P0: guardrail after_call records outcome for loop detection
                guard_ctrl.after_call(tool_name, tool_args, _result_str)

        # P0: Layer 3 — apply per-turn aggregate budget to this batch's results
        apply_turn_budget_to_batch(
            messages, batch_ids, batch_names, None,
            context_window_tokens=(
                endpoint.context_window if endpoint else None
            ),
        )

        # First iteration's query has executed — close out the exec sub-step.
        if iteration == 0:
            _prog_end("exec", "Executing query & fetching rows")

        # P0: if guardrail controller tripped a halt, inject nudge and break
        if guard_ctrl.halt_decision:
            _hd = guard_ctrl.halt_decision
            logger.warning(
                "Guardrail halt in sub-agent (data): %s (tool=%s, count=%d)",
                _hd.code, _hd.tool_name, _hd.count,
            )
            metrics.record_guardrail_halt(_hd.code)
            messages.append({
                "role": "user",
                "content": (
                    f"Tool '{_hd.tool_name}' is looping (count={_hd.count}). "
                    "Stop calling it and write your final answer now."
                ),
            })
            break
    else:
        # Hit max iterations without a final prose turn.  Don't set
        # final_text here — let the empty-answer guarantee below
        # produce a synthesis call (or the placeholder string).
        pass

    metrics.record_budget(conv_budget.used, conv_budget.max_total)

    # P0: one instrumentation line per delegation — wall time, iterations
    # used, LLM vs SQL split, truncation flag, row count. Lets ops answer
    # "why is the data turn slow" from a single log line.
    logger.info(
        "ask_data_agent delegation summary: conv=%s wall=%.1fs iters=%d "
        "llm=%.1fs sql=%.1fs truncated=%s rows=%d final_len=%d",
        (context or {}).get("conversation_id"),
        time.monotonic() - _delegation_start,
        iterations,
        _llm_total_s,
        _sql_total_s,
        _truncated,
        len(state["last_rows"]) if state.get("last_rows") else 0,
        len(final_text),
    )

    # ── NL2SQL auto-retry for trivial probes / garbage data ─────────────────
    # When the sub-agent ran trivial probes (SELECT * FROM table LIMIT 1) or
    # returned garbage data (ID-only columns), fall back to the NL2SQL
    # pipeline (NLAnswerService.answer) which has two-phase table selection
    # and zero-row auto-correction. This satisfies the requirement:
    # "prioritize NL2SQL services over knowledge graph or memory".
    _needs_nl2sql_retry = (
        state.get("trivial_probe_detected")
        or state.get("garbage_rows_detected")
        or (
            state.get("last_rows") is None
            and state.get("last_metadata_rows") is not None
            and not _truncated
        )
    )
    if _needs_nl2sql_retry and not final_text:
        logger.info(
            "ask_data_agent: detected trivial probe / garbage data, "
            "retrying with NL2SQL pipeline (trivial=%s, garbage=%s)",
            state.get("trivial_probe_detected"),
            state.get("garbage_rows_detected"),
        )
        try:
            _nl2sql_result = await _try_nl_fastpath(
                question, preferred_kb, bound_kb_ids, db, endpoint=endpoint,
                skip_narrate=bool((context or {}).get("main_agent_will_synthesize")),
            )
            if _nl2sql_result is not None and _nl2sql_result.get("success"):
                _nl2sql_rows = _nl2sql_result.get("rows") or []
                if _rows_have_business_data(_nl2sql_rows):
                    # NL2SQL retry succeeded with real business data
                    state["last_rows"] = _nl2sql_rows
                    state["last_sql"] = _nl2sql_result.get("sql")
                    if _nl2sql_result.get("source_id"):
                        state["source_id"] = _nl2sql_result["source_id"]
                    if _nl2sql_result.get("source_name"):
                        state["source_name"] = _nl2sql_result["source_name"]
                    final_text = (
                        f"DATA READY: Retrieved {len(_nl2sql_rows)} row(s) from "
                        f"{_nl2sql_result.get('source_name', 'the bound data source')}. "
                        f"Here are the first {min(len(_nl2sql_rows), _CONDENSED_MAX_ROWS)} rows:\n"
                        f"{_condensed_row_text(_nl2sql_rows)}\n"
                        "Use these rows to answer the user's question directly. "
                        "Do NOT reply with 'I had trouble' / 'I gathered some information "
                        "but...' — the data is here, write a concise answer now."
                    )
                    state["trivial_probe_detected"] = False
                    state["garbage_rows_detected"] = False
                    logger.info(
                        "ask_data_agent: NL2SQL retry succeeded with %d rows",
                        len(_nl2sql_rows),
                    )
                else:
                    logger.info(
                        "ask_data_agent: NL2SQL retry returned rows but no "
                        "business data, keeping original"
                    )
            else:
                logger.info(
                    "ask_data_agent: NL2SQL retry failed: %s",
                    (_nl2sql_result or {}).get("error", "no result"),
                )
        except Exception as _nl2sql_err:
            logger.warning(
                "ask_data_agent: NL2SQL retry raised (non-fatal): %s",
                _nl2sql_err,
            )

    # --- Empty-answer guarantee ---------------------------------------------
    # If the LLM stopped with tool_calls and the loop terminated without
    # the LLM writing any prose (common: it called execute_query, then
    # ran out of iterations), force one final synthesis call with NO
    # tools. This makes the `answer` field non-empty so the calling
    # agent always has prose to render in the chat bubble.
    #
    # When the wall-clock budget was hit (`_truncated`), skip the extra
    # synthesis LLM call — we are already over budget; the directive
    # fallback below fills `answer` from the captured rows instead.
    if (
        not final_text
        and not _truncated
        and state.get("last_rows") is not None
    ):
        logger.info(
            "ask_data_agent: forcing synthesis turn (rows=%d, prior iters=%d)",
            len(state["last_rows"]) if state["last_rows"] else 0,
            iterations,
        )
        try:
            rows_for_llm = (state["last_rows"] or [])[:100]
            synthesis_messages = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        "You have the data above. Stop calling tools and "
                        "write a concise prose answer now. Open with the "
                        "direct answer in one sentence, then add any "
                        "necessary breakdown (top values, totals, units, "
                        "source). Mention the source by name. Do NOT emit "
                        "any more tool calls.\n\n"
                        f"Rows you retrieved (first {len(rows_for_llm)}):\n"
                        f"```json\n{json.dumps(rows_for_llm, default=str, ensure_ascii=False)}\n```"
                    ),
                }
            ]
            # P1.3/P2: sanitize before the synthesis call too
            pre_call_prep(synthesis_messages)
            synthesis_response = await _call_llm_with_retry(synthesis_messages, tools=[], endpoint=endpoint)
            final_text = (synthesis_response.get("content", "") or "").strip()
            iterations += 1
        except Exception as e:
            # Synthesis failure is a SOFT failure: rows are already
            # captured, so the data was retrieved successfully. We log
            # and let the row-count fallback below produce a non-empty
            # `answer`. Crucially, we do NOT touch _initial_llm_failure
            # so `success` stays True.
            _synthesis_llm_failure = str(e)
            logger.warning("ask_data_agent synthesis turn failed (soft): %s", e)

    # If still empty, set a non-empty placeholder so the calling agent
    # never sees answer=None/"" for a successful data run. When the
    # initial LLM call failed, use a user-friendly message that the
    # calling LLM can quote verbatim (so it doesn't hallucinate
    # "I queried the database and found nothing").
    #
    # Two directive texts (NOT status messages): the calling model reads
    # `answer` and decides whether to synthesize. A passive status like
    # "Retrieved N row(s)" reads as a failure and produces the generic
    # "I had trouble putting it all together" apology. These directives
    # tell the calling model the data is ready (with actual values) and
    # instruct it to synthesize — never to apologize.
    if not final_text:
        if state.get("last_rows"):
            _rows = state["last_rows"]
            _src = state.get("source_name") or "the bound data source"
            _condensed = _condensed_row_text(_rows)
            final_text = (
                f"DATA READY: Retrieved {len(_rows)} row(s) from {_src}. "
                f"Here are the first {min(len(_rows), _CONDENSED_MAX_ROWS)} rows:\n"
                f"{_condensed}\n"
                "Use these rows to answer the user's question directly. "
                "Do NOT reply with 'I had trouble' / 'I gathered some information "
                "but...' — the data is here, write a concise answer now."
            )
        elif _initial_llm_failure:
            # Truncate the underlying error to avoid leaking internals.
            short_err = (_initial_llm_failure or "")[:120].strip() or "upstream error"
            final_text = (
                f"The Data Agent could not reach the language model "
                f"({short_err}). No data was retrieved. Please retry in a moment."
            )
        elif state.get("last_metadata_rows"):
            final_text = (
                "The Data Agent discovered table schemas but did not fetch any "
                "business data. Please retry or rephrase your question."
            )
        else:
            final_text = (
                "The Data Agent could not retrieve any business data rows. "
                "Possible reasons: (1) no data matches the filter criteria, "
                "(2) the table/column names may be incorrect, or (3) the "
                "query timed out. Tell the user specifically which of these "
                "is most likely and suggest a concrete next step. Do NOT say "
                "'I had trouble putting it all together' — that is not helpful."
            )

    # Resolve source name from the bound KB list
    if state["source_id"]:
        from app.models.knowledge_base import KnowledgeBase
        kb = (
            db.query(KnowledgeBase)
            .filter(KnowledgeBase.id == state["source_id"], KnowledgeBase.is_deleted == False)  # noqa: E712
            .first()
        )
        if kb:
            state["source_name"] = kb.name

    # --- Metadata-only honesty gate -----------------------------------------
    # If the sub-agent's last query returned ONLY date-range / row-count
    # aggregates (MIN/MAX/COUNT with no business dimensions), that is not a
    # real answer. Return a STRUCTURED failure so the calling agent sees
    # honest context ("query returned only metadata") instead of a
    # "success with metadata rows" that it would render as real data. The
    # rows are still passed through so the main loop's GoalContract can
    # count the metadata-only shape and force a real query on exit.
    _metadata_only = is_metadata_only_rows(state.get("last_rows"))
    if _metadata_only:
        final_text = (
            "The Data Agent's query returned only metadata (date range / "
            "row-count aggregates such as MIN/MAX/COUNT) without business "
            "data rows. Re-run the query selecting the actual business "
            "columns instead of wrapping them in MIN/MAX/COUNT."
        )
        _metadata_err = (
            "query returned only metadata (date range / row count) without "
            "business data rows"
        )

    # Cap the rows we hand back to the calling agent. The LLM only needs a
    # representative sample to synthesize a report; sending the full result
    # set (which can be 10k+ rows) just bloats the context window and slows
    # the next LLM turn. The condensed LLM view (~5 rows) is unaffected.
    ASK_DATA_MAX_RETURN_ROWS = 200
    if state.get("last_rows") and len(state["last_rows"]) > ASK_DATA_MAX_RETURN_ROWS:
        state["last_rows"] = state["last_rows"][:ASK_DATA_MAX_RETURN_ROWS]

    # Data quality metadata for the calling agent
    _data_quality = {
        "has_business_data": _rows_have_business_data(state.get("last_rows")),
        "trivial_probe_detected": state.get("trivial_probe_detected", False),
        "garbage_rows_detected": state.get("garbage_rows_detected", False),
        "row_count": len(state["last_rows"]) if state.get("last_rows") else 0,
    }

    # Derive column names from row dicts so downstream consumers
    # (_payload_from_execution, docx/pptx exporters) have schema metadata.
    _rows = state.get("last_rows")
    if _rows and isinstance(_rows[0], dict):
        _derived_columns = list(_rows[0].keys())
    else:
        _derived_columns = []

    return {
        "success": _initial_llm_failure is None and not _metadata_only,
        "error": (_metadata_err if _metadata_only else _initial_llm_failure),
        "error_kind": "metadata_only" if _metadata_only else None,
        "answer": final_text,
        "rows": state["last_rows"],
        "columns": _derived_columns,
        "sql": state["last_sql"],
        "source_id": state["source_id"],
        "source_name": state["source_name"],
        "iterations": iterations,
        "truncated": _truncated,
        "data_quality": _data_quality,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _try_nl_fastpath(
    question: str,
    preferred_kb: str | None,
    bound_kb_ids: list[str],
    db: Session,
    endpoint=None,
    skip_narrate: bool = False,
    agent_name: str | None = None,
) -> dict | None:
    """One-shot NL2SQL fast path for a single bound database source.

    Runs NLAnswerService (introspect → text2sql → execute → narrate = 2 LLM
    calls) and returns a fully-shaped ask_data_agent result on success.
    Returns None when the fast path does not apply or fails — the caller
    then falls back to the iterative sub-agent loop, which can self-correct
    SQL across iterations.
    """
    kb_id = preferred_kb or (bound_kb_ids[0] if len(bound_kb_ids) == 1 else None)
    if not kb_id:
        return None

    # Only database-kind KBs qualify — the bound set may include file sources.
    from app.models.knowledge_base import KnowledgeBase
    kb = (
        db.query(KnowledgeBase)
        .filter(KnowledgeBase.id == kb_id, KnowledgeBase.is_deleted == False)  # noqa: E712
        .first()
    )
    if kb is None or (kb.source_kind or "database").lower() != "database":
        return None

    try:
        from app.services.db import NLAnswerService
        result = await NLAnswerService(db).answer(kb_id, question, endpoint=endpoint, skip_narrate=skip_narrate, agent_name=agent_name)
    except Exception as e:
        logger.warning("ask_data_agent fast path raised (falling back to loop): %s", e)
        return None

    if not result.get("success"):
        logger.info(
            "ask_data_agent fast path failed (falling back to loop): %s",
            result.get("error"),
        )
        return None

    _fp_rows = result.get("rows")
    if _fp_rows and isinstance(_fp_rows[0], dict):
        _fp_cols = list(_fp_rows[0].keys())
    else:
        _fp_cols = []

    return {
        "success": True,
        "answer": result.get("answer") or "",
        "rows": _fp_rows,
        "columns": _fp_cols,
        "sql": result.get("sql"),
        "source_id": result.get("source_id") or kb_id,
        "source_name": result.get("source_name") or kb.name,
        "iterations": result.get("iterations", 1),
        "fastpath": True,
    }


# Generic zero-row recovery / schema-discovery rules for the Data Agent
# sub-loop. Applies to ALL databases — no app-specific column names here.
_GENERIC_DATA_AGENT_HINT_BLOCK = """

ZERO-ROW RECOVERY (HARD RULE — applies to ALL databases):
- If a query returns zero rows or all-NULL aggregates, do NOT report "no data"
  immediately. The data may exist in a different table or under different
  column names that you haven't discovered yet.
- The MOST COMMON cause of zero-row results is querying a STALE table (one
  whose data is outdated or only covers an older time period). ALWAYS check
  the FULL TABLE CATALOG in the schema output — it lists ALL tables with
  their row counts and coverage dates. Prefer tables with MORE rows and
  MORE RECENT coverage dates.
- Recovery steps:
  1. Look at the FULL TABLE CATALOG section in the schema output. It lists
     ALL tables with row counts and coverage dates. If the table you queried
     has few rows or old coverage (e.g. data ends 2025-12-31), try a DIFFERENT
     table that has more rows and newer coverage.
  2. Check data freshness: `SELECT MAX(<date_column>) FROM <table>` on the
     tables you queried — they may be stale or empty.
  3. Try alternative tables from the FULL TABLE CATALOG that could contain
     the requested data. Look for tables with:
     - Higher row counts (more data)
     - More recent coverage dates (e.g. data through 2026 vs 2025)
     - Names suggesting the right domain (sales → look for "sal", "order",
       "outstock" in table names; inventory → look for "inv", "stk")
  4. If values like product names or categories are filtering out rows, run
     `SELECT DISTINCT <column> FROM <table> LIMIT 200` to discover the
     actual stored values before filtering.
- NEVER assume you know all the tables in the database. Any database can have
  views, summary tables, or alternative schemas that contain the data you need.
- Different databases use different naming conventions. A column could be
  named `material_name`, `FNAME`, `product_title`, or anything else — always
  verify with `describe_schema`.
- IMPORTANT: When writing SQL, do NOT use reserved words as aliases (e.g.
  `rows`, `year`, `month`, `date`, `group`, `order`). Use safe aliases like
  `row_count`, `yr`, `mo`, `dt` instead. MySQL will reject reserved-word aliases.

ANSWER INTEGRITY (HARD RULE — applies to ALL databases):
- SCOPE: If the question is company/organization-wide ("top 5 customers",
  "revenue by region", "company total"), query the fact table/view with the
  BROADEST coverage. A table or view whose name or description indicates it is
  scoped to ONE org/region/plant is a SUBSET — never present a subset as the
  company-wide answer, and never use an org-scoped view to answer a
  company-wide question.
- ENTITY FILTER: For any ranking/grouping by an entity name (customer,
  supplier, product), ALWAYS filter out NULL/empty/placeholder values in the
  SQL (`WHERE <name_col> IS NOT NULL AND <name_col> != ''`). NEVER present a
  "(unknown)" / "[Customer name missing]" bucket as a ranked row. If the query
  result contains such a row, drop it and note the count in your narrative —
  do not fabricate an entity to fill the gap.
- METRIC LABELING: Pick ONE amount column for the revenue/value metric and
  state WHICH column you used (e.g. tax-exclusive vs tax-inclusive) in your
  narrative. Never silently switch metrics between rows or between the table
  and your prose. Do not present a tax-inclusive column as plain "revenue"
  without saying it is tax-inclusive.
- RECONCILIATION: Before writing any total or share percentage, verify it
  against the SQL evidence: (1) the sum of the rows you present must equal the
  total you quote; (2) every share percentage must equal row / total. If they
  do not reconcile, fix the query or the arithmetic — do not ship an
  inconsistent table. A stated "top-N total" that differs from the sum of the
  listed rows is a hard error.
- NO INVENTED FIGURES: Every number in your answer must trace to the executed
  query result. Never invent a total, a customer, a volume, or a percentage to
  make the story cleaner.
"""


def _build_sub_agent_prompt(
    question: str,
    bound_kb_ids: list[str],
    preferred_kb: str | None,
    caller: str | None = None,
) -> str:
    """Compose the system prompt the Data Agent sub-loop runs with."""
    from app.services.agent_definitions import DATA_AGENT_PROMPT

    bound_section = ""
    if bound_kb_ids:
        ids = ", ".join(bound_kb_ids)
        bound_section = (
            f"\n\nSCOPED DATA SOURCES (you may only use these)\n"
            f"Available data sources for this call: {ids}."
        )
        if preferred_kb:
            bound_section += f"\nThe caller suggested source: {preferred_kb!r}."

    prompt = DATA_AGENT_PROMPT + bound_section

    # P4: if the caller embedded a compact `[schema: ...]` block in the
    # question (see _build_schema_slice in data_source_runtime), surface it
    # as a supplementary schema hint. The sub-agent MUST still call
    # describe_schema first (mandatory), but the hint can help it focus on
    # the right tables and resolve ambiguities faster.
    schema_match = re.search(r"\[schema:[^\]]{0,1200}\]", question or "")
    if schema_match:
        prompt = (
            prompt
            + "\n\nSCHEMA HINT (supplementary — provided by the caller):\n"
            + schema_match.group(0)
            + "\nThis hint may help you identify relevant tables, but you MUST still "
            "call describe_schema first to verify actual column names and discover "
            "additional tables not covered by the hint."
        )

    # All callers: append the generic zero-row recovery rules so the
    # sub-agent knows to re-discover schema when queries return empty.
    prompt = prompt + _GENERIC_DATA_AGENT_HINT_BLOCK

    # C1c: static-route pinning for the sub-loop. The one-shot fast path
    # (NLAnswerService) applies query_router's pinned table + date_hint, but
    # when the fast path falls through to the iterative sub-agent loop the
    # route was NEVER injected — observed failure mode: a business term was
    # routed to a broad line-item table and the report showed meaningless
    # generic KPIs summed across unrelated rows. Resolve the same static
    # route here and surface the pinned table + hint so the sub-agent
    # anchors on the correct table.
    try:
        from app.services.db.query_router import resolve_static_route
        _route = resolve_static_route(question or "", agent_name=caller)
        if _route:
            _route_pin = (
                "\n\nSTATIC ROUTE PIN (authoritative — from the query router):\n"
                f"Use table `{_route['table']}` for this question. "
            )
            _fb = _route.get("fallback_tables") or []
            if _fb:
                _route_pin += f"If it lacks the needed columns, fall back to: {', '.join(_fb)}. "
            _route_pin += (
                "Do NOT pick a different table by name similarity — the pinned "
                "table is the known-correct one for this business intent.\n"
            )
            _hint = _route.get("date_hint")
            if _hint:
                _route_pin += "ROUTE HINT:\n" + _hint + "\n"
            prompt = prompt + _route_pin
    except Exception as e:  # noqa: BLE001 — route hint must never break the loop
        logger.debug("_build_sub_agent_prompt: static route hint skipped (non-fatal): %s", e)

    # Per-app domain hint (de-hardcoded): any app may ship a
    # domain_configs/<agent_name>.json "data_agent_hint" block. Empty for
    # apps without config — fully generic behavior.
    try:
        from app.services.domain_config import get_data_agent_hint
        _domain_hint = get_data_agent_hint(caller)
        if _domain_hint:
            prompt = prompt + "\n\nDOMAIN HINT (from app config):\n" + _domain_hint + "\n"
    except Exception as e:  # noqa: BLE001
        logger.debug("_build_sub_agent_prompt: domain hint skipped (non-fatal): %s", e)

    return prompt


def _build_sub_agent_tool_schemas() -> list[dict]:
    """Return the OpenAI-format tool schemas for the 4 DB tools."""
    out: list[dict] = []
    for name in _DATA_AGENT_TOOLS:
        entry = registry.get_entry(name)
        if entry and entry.schema:
            out.append(entry.schema)
    return out


def _is_metadata_query(sql: str | None) -> bool:
    """Return True if `sql` looks like a catalog/schema/metadata query."""
    if not sql:
        return False
    s = sql.strip().lower()
    # Use regex word boundaries for schema names to avoid matching
    # table names like `information_schema_mock`.
    metadata_re_patterns = [
        r"\binformation_schema\b",
        r"\bpg_catalog\b",
        r"\bpg_tables\b",
        r"\bpg_class\b",
        r"\bpg_namespace\b",
        r"\bmysql\.",
        r"\bsys\.",
        r"\bshow\s+full\s+tables\b",
        r"\bshow\s+tables\b",
        r"\bshow\s+full\s+columns\b",
        r"\bshow\s+columns\b",
        r"\bdescribe\s+",
        r"\bdesc\s+",
        r"\bexplain\s+",
    ]
    for pat in metadata_re_patterns:
        if re.search(pat, s):
            return True
    return False


# Keys that carry no user-facing business value and would only bloat the
# condensed row summary (FK/surrogate ids, row markers).
_SKIP_COLS = frozenset({"FID", "fid", "id", "ID", "rowid", "ROWID"})

# How many rows go into the condensed summary inside the `answer` field.
_CONDENSED_MAX_ROWS = 5


def _condensed_row_text(rows: list, max_rows: int = _CONDENSED_MAX_ROWS, max_chars: int = 500) -> str:
    """First N rows as compact key=value pairs.

    This is what the calling model actually sees in the `answer` field —
    even when the full tool-result JSON is persisted to disk (1,500-char
    preview), the condensed values ride along with `answer` so the caller
    always has real numbers to synthesize from.
    """
    if not rows:
        return "(no rows)"
    lines: list[str] = []
    for row in rows[:max_rows]:
        if isinstance(row, dict):
            pairs = [
                f"{k}={v}"
                for k, v in row.items()
                if k not in _SKIP_COLS and v is not None and v != ""
            ]
            lines.append("{" + ", ".join(pairs) + "}")
        else:
            lines.append(str(row))
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "…"
    return out


def _maybe_capture_execute_result(tool_name: str, result: dict, state: dict) -> None:
    """If this tool produced rows, remember them for the caller.

    Metadata/schema queries are stored separately so they do NOT poison
    `last_rows` and trigger the empty-answer synthesis fallback.

    Trivial probes (SELECT * FROM table LIMIT 1) and garbage data (rows
    containing only internal ID columns) are tracked separately so the
    empty-answer guarantee can detect them and trigger an NL2SQL retry.
    """
    if not isinstance(result, dict) or not result.get("success"):
        return
    sql = result.get("sql")
    is_meta = _is_metadata_query(sql)
    rows = result.get("rows")

    # Detect trivial probes and garbage business data
    is_trivial = _is_trivial_probe(sql) if sql else False
    is_garbage = _is_garbage_business_data(rows) if rows else False

    if tool_name == "execute_query":
        if is_meta or is_trivial:
            # Schema/metadata queries AND trivial probes go to metadata bucket
            state["last_metadata_rows"] = rows
            state["last_metadata_sql"] = sql
            if is_trivial:
                state["trivial_probe_detected"] = True
                state["trivial_probe_sql"] = sql
        elif is_garbage:
            # Garbage data (ID-only columns) — track but don't use as real rows
            state["garbage_rows_detected"] = True
            state["garbage_rows"] = rows
            state["garbage_sql"] = sql
            # Don't set last_rows — let the loop retry
        else:
            state["last_rows"] = rows
            state["last_sql"] = sql
        src = result.get("source") or {}
        if src.get("id"):
            state["source_id"] = src["id"]
        if src.get("name"):
            state["source_name"] = src["name"]
    elif tool_name == "answer_from_database":
        if is_meta:
            state["last_metadata_rows"] = rows
            state["last_metadata_sql"] = sql
        else:
            state["last_rows"] = rows
            state["last_sql"] = sql
        if result.get("source_id"):
            state["source_id"] = result["source_id"]
        if result.get("source_name"):
            state["source_name"] = result["source_name"]


def _rows_all_null(rows: list) -> bool:
    """True when every returned row is an all-NULL dict (aggregate query that
    matched nothing — e.g. wrong column name + English filter literal). Such
    rows are schema-shaped evidence of "no data", not real business data."""
    if not rows:
        return False
    for row in rows:
        if not isinstance(row, dict):
            return False
        if any(v is not None for v in row.values()):
            return False
    return True


async def _sub_loop_answer_gate(
    question: str,
    state: dict,
    final_text: str,
    *,
    attempts: int,
    budget_remaining: int,
    endpoint: Optional[str] = None,
) -> tuple[str, str]:
    """Universal Self-Evaluation gate for the Data Agent sub-loop.

    Runs the hybrid evaluator (deterministic-only unless both flags on) on
    the draft answer plus the accumulated query state. Returns ``(action,
    message)``:

    - ``("", "")`` — skipped (flag off / no tool evidence) or COMPLETE
    - ``("nudge", msg)`` — re-plan and continue the loop
    - ``("disclose", msg)`` — append the gap disclosure and break

    TOTAL: never raises and never blocks the loop on failure. Metadata-only
    evidence (no business rows ever captured) is represented as a
    schema-shaped payload so the metadata detector fires and the agent is
    nudged to run a real data query.
    """
    if not getattr(settings, "SELF_EVAL_REPLAN_ENABLED", False):
        return "", ""
    tool_results: list[dict] = []
    rows = state.get("last_rows")
    if rows is not None:
        if _rows_all_null(rows):
            # All-NULL aggregate rows (wrong column name + filter that
            # matched nothing) mean "no business data" — represent them as
            # columns + zero rows so the metadata-only detector fires and the
            # agent is nudged to re-probe instead of answering "no data".
            first = rows[0] if rows and isinstance(rows[0], dict) else {}
            tool_results.append({
                "tool": "execute_query",
                "columns": list(first.keys()),
                "row_count": 0,
                "rows": [],
            })
        else:
            tool_results.append({
                "tool": "execute_query",
                "rows": rows,
                "sql": state.get("last_sql"),
            })
    else:
        meta_rows = state.get("last_metadata_rows")
        if meta_rows is not None:
            # Schema-shaped payload: columns known but ZERO business rows,
            # so the metadata-only detector fires and nudges a real query.
            first = meta_rows[0] if meta_rows and isinstance(meta_rows[0], dict) else {}
            tool_results.append({
                "tool": "execute_query",
                "columns": list(first.keys()),
                "row_count": 0,
                "rows": [],
            })
    if not tool_results:
        # No tool evidence accumulated — nothing to verify against.
        return "", ""
    timeout_s = getattr(settings, "SELF_EVAL_LLM_GATE_TIMEOUT_S", 15.0)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                evaluate_answer,
                question,
                tool_results,
                final_text,
                attempts=attempts,
                budget_remaining=budget_remaining,
                endpoint=endpoint,
            ),
            timeout=timeout_s,
        )
    except Exception as exc:
        logger.warning("sub-loop answer-verification failed (non-fatal): %s", exc)
        return "", ""
    if result.status == "COMPLETE":
        return "", ""
    if result.status == "INCOMPLETE":
        return "nudge", build_replan_nudge(result)
    return "disclose", build_gap_disclosure(result)


async def _call_llm(
    messages: list[dict],
    tools: list[dict],
    *,
    endpoint=None,
) -> dict:
    """Single-turn LLM call (delegates to reliability wrapper).

    Uses ``temperature=0.2`` (lower than the generic sub-agent default of
    0.7) because the Data Agent should be deterministic when writing SQL.
    Prompt caching, error classification, and metrics are applied by
    :func:`call_llm_with_reliability`.

    Args:
        endpoint: Optional concrete ``LLMEndpoint`` from the project binding.
            Forwarded unchanged to ``call_llm_with_reliability``. When None,
            the legacy global provider is used.
    """
    return await call_llm_with_reliability(
        messages, tools, temperature=0.2, endpoint=endpoint
    )


# Errors that are worth retrying — they typically mean the upstream
# gateway / model had a momentary issue, not that our request is broken.
_RETRYABLE_HTTPX_ERRORS = (
    httpx.TimeoutException,
    httpx.ConnectError,
    httpx.NetworkError,
)


def _is_retryable_status_error(exc: BaseException) -> bool:
    """True if `exc` is an HTTPStatusError with a 5xx status code."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return isinstance(status, int) and 500 <= status < 600


async def _call_llm_with_retry(
    messages: list[dict],
    tools: list[dict],
    *,
    max_retries: int = 2,
    base_backoff: float = 0.5,
    endpoint=None,
) -> dict:
    """Call the LLM with bounded retry on transient errors.

    Retries on:
      - ``httpx.TimeoutException``  (slow upstream)
      - ``httpx.ConnectError``      (network blip)
      - ``httpx.NetworkError``      (DNS / socket)
      - ``httpx.HTTPStatusError`` with 5xx status

    Does NOT retry on 4xx (bad request / auth) — those will never succeed.

    Bounded latency: max_retries=2 + base_backoff=0.5 → at most 1.5 s of
    extra wall time in the worst case (0.5s + 1.0s backoff between 3
    attempts).

    Args:
        endpoint: Optional concrete ``LLMEndpoint`` forwarded to ``_call_llm``
            on every retry attempt.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await _call_llm(messages, tools, endpoint=endpoint)
        except Exception as e:
            last_exc = e
            is_retryable = (
                isinstance(e, _RETRYABLE_HTTPX_ERRORS) or _is_retryable_status_error(e)
            )
            if not is_retryable or attempt >= max_retries:
                # Either non-retryable (4xx, etc.) or out of retries.
                raise
            backoff = base_backoff * (2 ** attempt)
            logger.warning(
                "ask_data_agent LLM call failed (attempt %d/%d, class=%s); "
                "retrying in %.2fs: %s",
                attempt + 1, max_retries + 1,
                type(e).__name__, backoff, e,
            )
            await asyncio.sleep(backoff)
    # Should be unreachable, but be explicit.
    assert last_exc is not None
    raise last_exc


# ---------------------------------------------------------------------------
# Schema & Registration
# ---------------------------------------------------------------------------

ASK_DATA_AGENT_SCHEMA = {
    "type": "function",
    "function": {
        "name": "ask_data_agent",
        "description": (
            "Ask the builtin Data Agent a natural-language question about "
            "one of your bound data sources. The Data Agent is a specialist "
            "that inspects schema, writes SQL, and returns a structured "
            "payload (answer, rows, sql, source_id). Use it for any "
            "question that needs live database data.\n"
            "QUESTION FORMAT: pass a SHORT natural-language question "
            "(1-2 sentences). Do NOT embed raw SQL, full column lists, or "
            "join hints in the question — the Data Agent discovers the "
            "schema and writes its own SQL.\n"
            "PARALLEL OPTIMIZATION (HARD): if you need MULTIPLE independent "
            "data sets (e.g. sales volume AND revenue AND margin AND inventory "
            "for the same period), emit MULTIPLE ask_data_agent calls IN THE "
            "SAME response (one per independent question) — they execute in "
            "parallel and finish in the time of one call instead of N. This "
            "turns a 5-minute multi-query report into an ~80s one. Only "
            "serialize calls that genuinely depend on each other's output."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": (
                        "The natural-language question to answer. Keep it short "
                        "(1-2 sentences). The Data Agent will automatically "
                        "discover the schema before writing SQL."
                    ),
                },
                "data_source_id": {
                    "type": "string",
                    "description": (
                        "Optional. The id of the bound data source to query. "
                        "If omitted, the Data Agent will pick from your bound sources."
                    ),
                },
                "max_iterations": {
                    "type": "integer",
                    "description": "Max tool-calling iterations for the Data Agent (default 4, max 10). Needs at least 4 to allow schema discovery + query + zero-row recovery.",
                    "default": 4,
                },
            },
            "required": ["question"],
        },
    },
}

registry.register(
    name="ask_data_agent",
    schema=ASK_DATA_AGENT_SCHEMA,
    handler=_ask_data_agent,
    category="delegation",
    enabled_by_default=False,  # only enabled when KBs are bound
    description="Ask the builtin Data Agent a database question.",
)


# ===========================================================================
# fetch_data_batch — Direct parallel SQL execution (no sub-agent loop)
# ===========================================================================

async def _fetch_data_batch(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Execute multiple SQL queries in parallel against bound data sources.

    This is the FAST path: no LLM sub-agent loop, just direct SQL execution.
    Use ONLY when the Data Concepts catalog provides the exact table and column
    names. Falls back to ask_data_agent if schema is unknown or queries fail.
    """
    import re as _re_sql

    queries = args.get("queries", [])
    if not queries:
        return {"success": False, "error": "queries list is required"}

    if len(queries) > 8:
        return {"success": False, "error": "Maximum 8 queries per batch"}

    _ctx = context or {}

    # Reject template/placeholder SQL tokens. These appear when the LLM
    # copies documentation syntax ([table], [field]) into SQL. Such queries
    # always return wrong data and waste compute. Fail fast and redirect to
    # ask_data_agent which discovers the real schema.
    _PLACEHOLDER_PATTERNS = [
        (r'\[field\]',  'placeholder "[field]" — replace with actual column name from the catalog'),
        (r'\[table\]',  'placeholder "[table]" — replace with actual table name from the catalog'),
        (r'\[column\]', 'placeholder "[column]" — replace with actual column name'),
        (r'\[name\]',   'placeholder "[name]" — replace with actual identifier'),
        (r'\[value\]',  'placeholder "[value]" — replace with actual value'),
        (r'\bXXX\b',    'placeholder "XXX" — replace with actual identifier'),
        (r'\bTODO\b',   'placeholder "TODO" — replace with actual identifier'),
    ]

    # Validate each query: must be SELECT-only and reference known tables
    from app.services.nl2sql.schema_validator import check_read_only_sql
    from app.services.tool_handlers.db_tools import _execute_query, _require_kb_id

    validated: list[dict] = []
    for i, q in enumerate(queries):
        sql = (q.get("sql") or "").strip()
        data_source_id = q.get("data_source_id") or _ctx.get("kb_id")
        label = q.get("label") or f"query_{i+1}"

        if not sql:
            validated.append({"label": label, "valid": False, "error": "empty SQL"})
            continue

        # Reject template/placeholder syntax
        placeholder_err = None
        for pat, desc in _PLACEHOLDER_PATTERNS:
            if _re_sql.search(pat, sql, _re_sql.IGNORECASE):
                placeholder_err = desc
                break
        if placeholder_err:
            validated.append({
                "label": label,
                "valid": False,
                "error": f"SQL contains {placeholder_err}",
                "sql": sql,
                "hint": "Use ask_data_agent — it discovers the correct schema automatically.",
            })
            continue

        # Read-only check
        ro_error = check_read_only_sql(sql)
        if ro_error:
            validated.append({"label": label, "valid": False, "error": ro_error, "sql": sql})
            continue

        # Require a data source
        if not data_source_id:
            # Try to get from bound KBs in context
            bound_ids = _ctx.get("bound_kb_ids", [])
            if len(bound_ids) == 1:
                data_source_id = bound_ids[0]
            else:
                # Enumerate the bound sources so the LLM can pick the right
                # one instead of guessing (observed: the agent guessed the
                # demo source and built a deliverable from demo data while
                # the real ERP was bound).
                from app.models.knowledge_base import KnowledgeBase

                _src_names: list[str] = []
                if bound_ids:
                    try:
                        _rows = (
                            db.query(KnowledgeBase)
                            .filter(
                                KnowledgeBase.id.in_(bound_ids),
                                KnowledgeBase.is_deleted == False,  # noqa: E712
                            )
                            .all()
                        )
                        _by_id = {r.id: r for r in _rows}
                        for _bid in bound_ids:
                            _kb = _by_id.get(_bid)
                            if _kb is not None:
                                _db = f" ({_kb.database_name})" if _kb.database_name else ""
                                _src_names.append(f"{_kb.name}{_db} (id={_bid})")
                            else:
                                _src_names.append(f"<unknown> (id={_bid})")
                    except Exception:  # noqa: BLE001 — best-effort naming
                        _src_names = [f"(id={b})" for b in bound_ids]
                _src_help = "; ".join(_src_names) if _src_names else ", ".join(bound_ids)
                validated.append({
                    "label": label,
                    "valid": False,
                    "error": (
                        "data_source_id required (multiple bound data sources). "
                        f"Available bound sources: {_src_help}. "
                        "Pass the matching data_source_id explicitly."
                    ),
                })
                continue

        validated.append({
            "label": label,
            "valid": True,
            "sql": sql,
            "data_source_id": data_source_id,
            "max_rows": int(q.get("max_rows", 500)),
        })

    valid_queries = [v for v in validated if v.get("valid")]
    if not valid_queries:
        any_placeholder = any(
            "placeholder" in (v.get("error") or "")
            for v in validated
        )
        return {
            "success": False,
            "error": "No valid queries after validation",
            "validation": validated,
            "hint": (
                "SQL contained template placeholders like [field] or [table]. "
                "These come from documentation syntax, not real schema. "
                "Use ask_data_agent instead — it discovers correct tables/columns."
            ) if any_placeholder else (
                "Use ask_data_agent instead when table/column names are unknown."
            ),
        }

    # Execute all valid queries in parallel
    async def _run_one(q: dict) -> dict:
        start = time.monotonic()
        try:
            result = await _execute_query(
                args={
                    "sql": q["sql"],
                    "data_source_id": q["data_source_id"],
                    "max_rows": q["max_rows"],
                    "timeout_s": 15,
                },
                db=db,
                user_id=user_id,
                context={**_ctx, "kb_id": q["data_source_id"]},
            )
            elapsed = time.monotonic() - start
            return {
                "label": q["label"],
                "success": result.get("success", False),
                "rows": result.get("rows", []),
                "columns": result.get("columns", []),
                "sql": q["sql"],
                "row_count": len(result.get("rows", [])),
                "elapsed_s": round(elapsed, 1),
                "error": result.get("error"),
            }
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.warning("fetch_data_batch query '%s' failed: %s", q["label"], exc)
            return {
                "label": q["label"],
                "success": False,
                "error": str(exc),
                "elapsed_s": round(elapsed, 1),
            }

    results = await asyncio.gather(*[_run_one(q) for q in valid_queries])

    # Build summary
    total_rows = sum(r.get("row_count", 0) for r in results)
    success_count = sum(1 for r in results if r.get("success"))
    failed_count = len(results) - success_count
    total_elapsed = max(r.get("elapsed_s", 0) for r in results)

    return {
        "success": success_count > 0,
        "results": results,
        "validation": validated,
        "summary": {
            "total_queries": len(queries),
            "valid_queries": len(valid_queries),
            "successful": success_count,
            "failed": failed_count,
            "total_rows": total_rows,
            "wall_clock_s": round(total_elapsed, 1),
        },
        "hint": "If any queries failed, use ask_data_agent for schema discovery and retry." if failed_count > 0 else None,
    }


FETCH_DATA_BATCH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "fetch_data_batch",
        "description": (
            "Execute multiple SQL queries in parallel against bound data sources. "
            "This is the FAST path (1-5s total) — no sub-agent loop. "
            "Use ONLY when the Data Concepts catalog lists the exact table and column names "
            "you need. Example: if the catalog shows 'sales→sales_table, "
            "volume→qty_col, revenue→amount_col', you can write SQL directly "
            "using those names. If schema is unknown, use ask_data_agent instead.\n"
            "BENEFIT: 50-100x faster than ask_data_agent for known schemas.\n"
            "RULES: SELECT only, max 8 queries per call, max 500 rows per query."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "queries": {
                    "type": "array",
                    "description": (
                        "List of SQL queries to execute in parallel. Each query must "
                        "reference tables and columns from the Data Concepts catalog."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "sql": {
                                "type": "string",
                                "description": "SELECT statement. Tables/columns must match the Data Concepts catalog.",
                            },
                            "data_source_id": {
                                "type": "string",
                                "description": "ID of the bound data source. Omit if only one data source is bound.",
                            },
                            "label": {
                                "type": "string",
                                "description": "Short label for this query (e.g. 'sales_volume_july').",
                            },
                            "max_rows": {
                                "type": "integer",
                                "description": "Max rows to return (default 500).",
                                "default": 500,
                            },
                        },
                        "required": ["sql", "label"],
                    },
                    "minItems": 1,
                    "maxItems": 8,
                },
            },
            "required": ["queries"],
        },
    },
}

registry.register(
    name="fetch_data_batch",
    schema=FETCH_DATA_BATCH_SCHEMA,
    handler=_fetch_data_batch,
    category="delegation",
    enabled_by_default=False,  # only enabled when KBs are bound
    description="Execute multiple SQL queries in parallel (fast path, no sub-agent).",
)
