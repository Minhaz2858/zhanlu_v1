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
import re
import uuid
from typing import Any, Optional

import httpx
from sqlalchemy.orm import Session

from app.config import settings
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
    "ask_data_agent", "delegate_task", "memory", "send_message",
    "create_agent", "update_agent", "create_skill", "update_skill",
    "create_automation", "update_automation",
}


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
                not in (ResourceRoute.DATABASE, ResourceRoute.MULTI_RESOURCE)
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
            question, preferred_kb, bound_kb_ids, db, endpoint=endpoint
        )
        if fastpath_result is not None:
            return fastpath_result

    system_prompt = _build_sub_agent_prompt(question, bound_kb_ids, preferred_kb)
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

    iterations = 0
    # Default 4 iterations (was 2). The Data Agent now follows a
    # schema-discover -> query -> correct -> synthesize pattern: (1) call
    # `describe_schema`, (2) execute the query, (3) detect bad/null data and
    # re-query with corrected columns, (4) write the prose answer. A budget
    # of 2 leaves no room for self-correction, which caused the agent to
    # report "date coverage: None" instead of re-querying. The cap of 10 is
    # preserved for complex multi-table queries.
    max_iterations = min(args.get("max_iterations", 4), 10)
    final_text = ""
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

        # P1.3/P2: pre-API pruning + message sanitization
        pre_call_prep(messages)

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

        content = llm_response.get("content", "") or ""
        raw_tool_calls = llm_response.get("tool_calls", []) or []

        if not raw_tool_calls:
            final_text = content
            break

        # Append assistant message
        messages.append({
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
        })

        # P0: per-tool guardrail + per-result persistence
        batch_ids: list[str] = []
        batch_names: list[str] = []

        # --- Parallel execution for independent schema-discovery tools ---
        # When the LLM emits multiple tool calls in a single turn (e.g.
        # list_data_sources + describe_schema), run them concurrently via
        # asyncio.gather instead of sequentially. This cuts 1-2 turns of
        # latency for schema-discovery questions. Only read-only tools
        # that don't depend on each other's results are parallelized.
        _PARALLEL_SAFE_TOOLS = {"list_data_sources", "describe_schema", "search_documents"}

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
            _results = await asyncio.gather(*_coros, return_exceptions=True)

            for (tn, ta, tcid), result in zip(_parsed_calls, _results):
                batch_ids.append(tcid)
                batch_names.append(tn)
                if isinstance(result, Exception):
                    result = {"success": False, "error": str(result)}
                _maybe_capture_execute_result(tn, result, state)
                _result_str = persist_result_str(tn, result, None)
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
                    result = await execute_tool(
                        tool_name, tool_args, db, user_id, context=sub_context
                    )
                    _maybe_capture_execute_result(tool_name, result, state)

                # P0: Layer 2 per-result persistence (large results → disk preview)
                _result_str = persist_result_str(tool_name, result, None)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": _result_str,
                })
                # P0: guardrail after_call records outcome for loop detection
                guard_ctrl.after_call(tool_name, tool_args, _result_str)

        # P0: Layer 3 — apply per-turn aggregate budget to this batch's results
        apply_turn_budget_to_batch(messages, batch_ids, batch_names, None)

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

    # --- Empty-answer guarantee ---------------------------------------------
    # If the LLM stopped with tool_calls and the loop terminated without
    # the LLM writing any prose (common: it called execute_query, then
    # ran out of iterations), force one final synthesis call with NO
    # tools. This makes the `answer` field non-empty so the calling
    # agent always has prose to render in the chat bubble.
    if not final_text and state.get("last_rows") is not None:
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
    if not final_text:
        if state.get("last_rows"):
            final_text = (
                f"Retrieved {len(state['last_rows'])} row(s) from "
                f"{state.get('source_name') or 'the bound data source'}."
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
            final_text = "The Data Agent did not return a final answer."

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

    return {
        "success": _initial_llm_failure is None,
        "error": _initial_llm_failure,
        "answer": final_text,
        "rows": state["last_rows"],
        "sql": state["last_sql"],
        "source_id": state["source_id"],
        "source_name": state["source_name"],
        "iterations": iterations,
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
        result = await NLAnswerService(db).answer(kb_id, question, endpoint=endpoint)
    except Exception as e:
        logger.warning("ask_data_agent fast path raised (falling back to loop): %s", e)
        return None

    if not result.get("success"):
        logger.info(
            "ask_data_agent fast path failed (falling back to loop): %s",
            result.get("error"),
        )
        return None

    return {
        "success": True,
        "answer": result.get("answer") or "",
        "rows": result.get("rows"),
        "sql": result.get("sql"),
        "source_id": result.get("source_id") or kb_id,
        "source_name": result.get("source_name") or kb.name,
        "iterations": result.get("iterations", 1),
        "fastpath": True,
    }


def _build_sub_agent_prompt(
    question: str, bound_kb_ids: list[str], preferred_kb: str | None
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

    return DATA_AGENT_PROMPT + bound_section


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


def _maybe_capture_execute_result(tool_name: str, result: dict, state: dict) -> None:
    """If this tool produced rows, remember them for the caller.

    Metadata/schema queries are stored separately so they do NOT poison
    `last_rows` and trigger the empty-answer synthesis fallback.
    """
    if not isinstance(result, dict) or not result.get("success"):
        return
    sql = result.get("sql")
    is_meta = _is_metadata_query(sql)
    if tool_name == "execute_query":
        if is_meta:
            state["last_metadata_rows"] = result.get("rows")
            state["last_metadata_sql"] = sql
        else:
            state["last_rows"] = result.get("rows")
            state["last_sql"] = sql
        src = result.get("source") or {}
        if src.get("id"):
            state["source_id"] = src["id"]
        if src.get("name"):
            state["source_name"] = src["name"]
    elif tool_name == "answer_from_database":
        if is_meta:
            state["last_metadata_rows"] = result.get("rows")
            state["last_metadata_sql"] = sql
        else:
            state["last_rows"] = result.get("rows")
            state["last_sql"] = sql
        if result.get("source_id"):
            state["source_id"] = result["source_id"]
        if result.get("source_name"):
            state["source_name"] = result["source_name"]


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
            "question that needs live database data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "description": "The natural-language question to answer.",
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
                    "description": "Max tool-calling iterations for the Data Agent (default 4, max 10).",
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
