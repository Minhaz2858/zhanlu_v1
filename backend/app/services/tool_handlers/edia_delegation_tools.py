"""EDIA delegation tools — delegate questions to EDIA pipeline sub-agents.

Reuses the conversation-loop pattern from ``delegation_tools.py`` but
generalised for all EDIA pipeline agents:

- ask_forecast_agent   → forecast_agent
- ask_report_agent     → report_agent
- ask_perception       → perception_agent
- ask_rag_research     → rag_research_agent
- ask_diagnosis        → diagnosis_agent
- ask_pricing          → pricing_agent
- ask_orchestrator     → orchestrator_agent

Each tool looks up the agent's ``AgentDefinition`` (system_prompt + tools)
from ``BUILTIN_AGENTS``, builds the tool schemas, runs a sub-agent
conversation loop, and returns the final prose answer.

Return shape (consumed by the calling agent):

    {
        "success": True,
        "answer":    "<prose narrative>",
        "agent":     "<agent_name>",
        "iterations": N,
    }
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Callable

from sqlalchemy.orm import Session

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


# Tools that NO EDIA sub-agent is allowed to call recursively — prevents
# infinite delegation loops (agent A calls agent B which calls agent A …).
_DENIED_RECURSIVE = {
    "ask_orchestrator", "ask_perception", "ask_rag_research",
    "ask_diagnosis", "ask_pricing", "ask_forecast_agent",
    "ask_report_agent", "ask_data_agent", "ask_intelligence",
    "ask_knowledge_graph", "ask_decision", "ask_macro_override",
    "ask_erp_kpi", "ask_erp_writeback",
    "ask_perception_intelligence_diagnosis",
    "ask_forecast_pricing",
    "delegate_task",
    "create_agent", "update_agent", "create_skill", "update_skill",
    "create_automation", "update_automation", "send_message",
}


# ---------------------------------------------------------------------------
# Generic sub-agent runner
# ---------------------------------------------------------------------------

async def _run_sub_agent(
    agent_name: str,
    question: str,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
    max_iterations: int = 5,
) -> dict:
    """Run a named BUILTIN_AGENT in a sub-conversation loop.

    Parameters
    ----------
    agent_name : str
        Name of the agent in ``BUILTIN_AGENTS`` (e.g. ``"perception_agent"``).
    question : str
        The natural-language question to ask the sub-agent.
    db : Session
        Database session for tool execution.
    user_id : str | None
        User ID for permission checks.
    context : dict | None
        Agent runtime context (org_id, app_id, bound_kb_ids, etc.).
    max_iterations : int
        Max tool-calling iterations (default 8).
    """
    from app.services.agent_definitions import get_agent_definition

    agent_def = get_agent_definition(agent_name)
    if agent_def is None:
        return {
            "success": False,
            "error": f"Agent definition '{agent_name}' not found in BUILTIN_AGENTS.",
            "answer": "",
            "agent": agent_name,
            "iterations": 0,
        }

    # Build the system prompt
    system_prompt = agent_def.system_prompt or ""
    ctx = context or {}
    bound_kb_ids = list(ctx.get("bound_kb_ids") or [])
    if bound_kb_ids:
        system_prompt += (
            f"\n\nSCOPED DATA SOURCES (you may only use these)\n"
            f"Available data sources: {', '.join(bound_kb_ids)}"
        )

    # Build tool schemas for the agent's allowed tools
    allowed_tools = agent_def.tools or []
    tool_schemas: list[dict] = []
    for tname in allowed_tools:
        entry = registry.get_entry(tname)
        if entry and entry.schema:
            tool_schemas.append(entry.schema)

    # --- P1 Harness path ---
    try:
        from app.config import settings
        use_harness = settings.AGENT_HARNESS_ENABLED
    except Exception:
        use_harness = False

    if use_harness:
        return await _run_sub_agent_via_harness(
            agent_name=agent_name,
            question=question,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            tool_schemas=tool_schemas,
            denied_tools=_DENIED_RECURSIVE,
            db=db,
            user_id=user_id,
            context=ctx,
            max_iterations=max_iterations,
        )

    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": question},
    ]

    guard_ctrl = ToolLoopGuardController()
    conv_budget = IterationBudget(max_total=max_iterations)
    iterations = 0
    final_text = ""

    sub_context = dict(ctx)

    for iteration in range(max_iterations):
        if not conv_budget.consume():
            logger.info(
                "EDIA sub-agent (%s) iteration budget exhausted (%d/%d), breaking",
                agent_name, conv_budget.used, conv_budget.max_total,
            )
            break
        iterations += 1

        pre_call_prep(messages)

        try:
            llm_response = await call_llm_with_reliability(
                messages, tool_schemas, temperature=0.3,
            )
        except Exception as e:
            logger.warning("EDIA sub-agent (%s) LLM call failed on iter %d: %s", agent_name, iteration, e)
            final_text = (
                f"The {agent_name} could not reach the language model "
                f"({str(e)[:120]}). Please retry in a moment."
            )
            return {
                "success": False,
                "error": str(e),
                "answer": final_text,
                "agent": agent_name,
                "iterations": iterations,
            }

        content = llm_response.get("content", "") or ""
        raw_tool_calls = llm_response.get("tool_calls", []) or []

        if not raw_tool_calls:
            final_text = content
            break

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

        batch_ids: list[str] = []
        batch_names: list[str] = []
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

            _gd = guard_ctrl.before_call(tool_name, tool_args)
            if not _gd.allows_execution:
                result = json.loads(synthetic_blocked_result(_gd))
            elif tool_name in _DENIED_RECURSIVE:
                result = {
                    "success": False,
                    "error": f"Tool {tool_name!r} is not available to the {agent_name}.",
                }
            elif tool_name not in allowed_tools:
                result = {
                    "success": False,
                    "error": f"Tool {tool_name!r} is not available to the {agent_name}.",
                }
            else:
                from app.services.agent_tools import execute_tool
                result = await execute_tool(
                    tool_name, tool_args, db, user_id, context=sub_context
                )

            _result_str = persist_result_str(tool_name, result, None)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": _result_str,
            })
            guard_ctrl.after_call(tool_name, tool_args, _result_str)

        apply_turn_budget_to_batch(messages, batch_ids, batch_names, None)

        if guard_ctrl.halt_decision:
            _hd = guard_ctrl.halt_decision
            logger.warning(
                "Guardrail halt in EDIA sub-agent (%s): %s (tool=%s, count=%d)",
                agent_name, _hd.code, _hd.tool_name, _hd.count,
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

    metrics.record_budget(conv_budget.used, conv_budget.max_total)

    # Empty-answer guarantee: force a synthesis turn if the loop ended
    # without prose but with tool results in context.
    if not final_text:
        logger.info(
            "EDIA sub-agent (%s): forcing synthesis turn (prior iters=%d)",
            agent_name, iterations,
        )
        try:
            synthesis_messages = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        "You have the data above. Stop calling tools and "
                        "write a concise prose answer now. Open with the "
                        "direct answer in one sentence, then add any "
                        "necessary breakdown. Do NOT emit any more tool calls."
                    ),
                }
            ]
            pre_call_prep(synthesis_messages)
            synthesis_response = await call_llm_with_reliability(
                synthesis_messages, tools=[], temperature=0.3,
            )
            final_text = (synthesis_response.get("content", "") or "").strip()
            iterations += 1
        except Exception as e:
            logger.warning("EDIA sub-agent (%s) synthesis turn failed (soft): %s", agent_name, e)

    if not final_text:
        final_text = f"The {agent_name} did not return a final answer."

    return {
        "success": True,
        "answer": final_text,
        "agent": agent_name,
        "iterations": iterations,
    }


async def _run_sub_agent_via_harness(
    *,
    agent_name: str,
    question: str,
    system_prompt: str,
    allowed_tools: list[str],
    tool_schemas: list[dict],
    denied_tools: set[str],
    db: Session,
    user_id: str | None,
    context: dict | None = None,
    max_iterations: int = 8,
) -> dict:
    """Run a sub-agent through the P1 unified harness.

    Bridges EDIA's ``call_llm_with_reliability`` and guardrail hooks
    into the ``AgentRunOrchestrator`` so the loop is standardised while
    preserving EDIA guard behaviour.
    """
    ctx = context or {}

    # Bridge llm_fn: EDIA signature → orchestrator signature
    async def _harness_llm(
        messages: list[dict],
        tools: list[dict] | None,
        temperature: float,
    ) -> dict:
        return await call_llm_with_reliability(
            messages, tools or [], temperature=temperature,
        )

    # Bridge tool dispatcher: orchestrator signature → execute_tool
    async def _harness_dispatcher(
        tool_name: str,
        tool_args: dict,
        _db: Any,
        _user_id: str | None,
        _context: dict | None,
    ) -> dict:
        from app.services.agent_tools import execute_tool
        return await execute_tool(
            tool_name, tool_args, db, user_id, context=ctx,
        )

    from app.services.harness.orchestrator import AgentRunOrchestrator

    orch = AgentRunOrchestrator(
        agent_name=agent_name,
        task=question,
        system_prompt=system_prompt,
        tool_schemas=tool_schemas,
        allowed_tools=set(allowed_tools),
        llm_fn=_harness_llm,
        tool_dispatcher=_harness_dispatcher,
        db=db,
        user_id=user_id,
        context=ctx,
        denied_tools=denied_tools,
        max_iterations=max_iterations,
        pre_call_prep=pre_call_prep,
        persist_result_str=lambda tn, r: persist_result_str(tn, r, None),
        apply_turn_budget=lambda msgs, ids, names: apply_turn_budget_to_batch(
            msgs, ids, names, None
        ),
    )
    result = await orch.run()
    return {
        "success": result.success,
        "answer": result.answer,
        "agent": agent_name,
        "iterations": result.iterations,
    }


# ---------------------------------------------------------------------------
# Individual tool handlers
# ---------------------------------------------------------------------------

async def _ask_forecast_agent(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Delegate a forecasting question to the forecast_agent."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    return await _run_sub_agent("forecast_agent", question, db, user_id, context)


async def _ask_report_agent(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Delegate a report generation request to the report_agent."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    return await _run_sub_agent("report_agent", question, db, user_id, context)


async def _ask_perception(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Delegate a market perception question to the perception_agent."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    return await _run_sub_agent("perception_agent", question, db, user_id, context)


async def _ask_rag_research(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Delegate a RAG research question to the rag_research_agent."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    return await _run_sub_agent("rag_research_agent", question, db, user_id, context)


async def _ask_diagnosis(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Delegate a market diagnosis question to the diagnosis_agent."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    return await _run_sub_agent("diagnosis_agent", question, db, user_id, context)


async def _ask_pricing(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Delegate a pricing recommendation request to the pricing_agent."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    return await _run_sub_agent("pricing_agent", question, db, user_id, context)


async def _ask_orchestrator(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Delegate a full EDIA pipeline request to the ecisco_bi_assistant."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    return await _run_sub_agent("ecisco_bi_assistant", question, db, user_id, context, max_iterations=15)


async def _ask_intelligence(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Delegate an intelligence question to the intelligence_agent."""
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}
    return await _run_sub_agent("intelligence_agent", question, db, user_id, context)


async def _ask_perception_intelligence_diagnosis(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Run perception + intelligence + diagnosis sub-agents in parallel (batch).

    The three sub-agents are independent: market snapshot, market
    intelligence context, and driver diagnosis. Running them concurrently
    with ``asyncio.gather`` replaces three sequential 226s tool calls with a
    single ~226s tool call, cutting weekly-report wall time roughly 3x.

    Returns a combined dict:

        {
            "success": True,
            "perception":   {…},   # raw sub-agent result
            "intelligence": {…},   # raw sub-agent result
            "diagnosis":    {…},   # raw sub-agent result
            "answer": "<concatenated prose for the calling agent>",
            "agent": "perception+intelligence+diagnosis",
        }
    """
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}

    import asyncio

    perception, intelligence, diagnosis = await asyncio.gather(
        _run_sub_agent("perception_agent", question, db, user_id, context),
        _run_sub_agent("intelligence_agent", question, db, user_id, context),
        _run_sub_agent("diagnosis_agent", question, db, user_id, context),
    )

    sections = []
    for label, result in (
        ("PERCEPTION", perception),
        ("INTELLIGENCE", intelligence),
        ("DIAGNOSIS", diagnosis),
    ):
        ok = bool(result and result.get("success"))
        text = (result or {}).get("answer") or ""
        if ok and text:
            sections.append(f"[{label}]\n{text}")
        else:
            err = (result or {}).get("error") or "no result"
            sections.append(f"[{label}]\n({label.lower()} failed: {err})")

    return {
        "success": True,
        "perception": perception,
        "intelligence": intelligence,
        "diagnosis": diagnosis,
        "answer": "\n\n".join(sections),
        "agent": "perception+intelligence+diagnosis",
    }


async def _ask_forecast_pricing(
    args: dict,
    db: Session,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Run forecast + pricing sub-agents in parallel (batch).

    The two sub-agents are independent: an ensemble forecast of
    forecastable products and a 9-step pricing recommendation. Running
    them concurrently with ``asyncio.gather`` replaces two sequential
    long tool calls with a single one, cutting weekly-report wall time
    roughly in half for Step 3.

    Returns a combined dict:

        {
            "success": True,
            "forecast": {…},   # raw sub-agent result
            "pricing":  {…},   # raw sub-agent result
            "answer": "<concatenated prose for the calling agent>",
            "agent": "forecast+pricing",
        }
    """
    question = (args.get("question") or "").strip()
    if not question:
        return {"success": False, "error": "question is required"}

    import asyncio

    forecast, pricing = await asyncio.gather(
        _run_sub_agent("forecast_agent", question, db, user_id, context),
        _run_sub_agent("pricing_agent", question, db, user_id, context),
    )

    sections = []
    for label, result in (
        ("FORECAST", forecast),
        ("PRICING", pricing),
    ):
        ok = bool(result and result.get("success"))
        text = (result or {}).get("answer") or ""
        if ok and text:
            sections.append(f"[{label}]\n{text}")
        else:
            err = (result or {}).get("error") or "no result"
            sections.append(f"[{label}]\n({label.lower()} failed: {err})")

    return {
        "success": True,
        "forecast": forecast,
        "pricing": pricing,
        "answer": "\n\n".join(sections),
        "agent": "forecast+pricing",
    }


async def _ask_knowledge_graph(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Direct knowledge graph query via Python service (no sub-agent delegation)."""
    from app.services.knowledge_graph.tool import _handle_knowledge_graph
    return await _handle_knowledge_graph(args, db, user_id, context)


async def _ask_decision(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Direct decision synthesis via Python service (no sub-agent delegation)."""
    from app.services.decision.tool import _handle_ask_decision
    return await _handle_ask_decision(args, db, user_id, context)


async def _ask_macro_override(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Direct macro override via Python service (no sub-agent delegation)."""
    from app.services.pricing.tool import _handle_ask_macro_override
    return await _handle_ask_macro_override(args, db, user_id, context)


async def _ask_erp_kpi(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """ERP KPI query via direct Python service (no sub-agent delegation)."""
    from app.services.erp.tool import _handle_ask_erp_kpi
    return await _handle_ask_erp_kpi(args, db, user_id, context)


async def _ask_erp_writeback(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """ERP writeback via direct Python service (no sub-agent delegation)."""
    from app.services.erp.tool import _handle_ask_erp_writeback
    return await _handle_ask_erp_writeback(args, db, user_id, context)


# ---------------------------------------------------------------------------
# Schemas & Registration
# ---------------------------------------------------------------------------

def _make_schema(name: str, description: str, agent_label: str) -> dict:
    """Build an OpenAI-format tool schema for a delegation tool."""
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": f"The natural-language question to ask the {agent_label}.",
                    },
                    "async": {
                        "type": "boolean",
                        "description": (
                            "If true, the task is enqueued for background "
                            "execution and a run_id is returned immediately. "
                            "Collect the result later with collect_run(). "
                            "Default: false (blocking synchronous call)."
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    }


def _maybe_async(
    handler: Callable,
) -> Callable:
    """Wrap a handler so it respects ``async: true`` when the
    ``EDIA_DELEGATION_ASYNC_ENABLED`` config flag is set.

    Returns the normal sync result when async is false / flag off.
    When both conditions are true, enqueues a background run and
    returns ``{success: True, run_id: "...", status: "queued"}``.
    """
    async def _wrapped(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
        want_async = bool(args.get("async", False))
        try:
            from app.config import settings
            async_enabled = settings.EDIA_DELEGATION_ASYNC_ENABLED
        except Exception:
            async_enabled = False

        if want_async and async_enabled:
            # Build a unique run_id and enqueue for background execution
            import uuid as _uuid
            run_id = _uuid.uuid4().hex[:32]
            ctx = context or {}
            agent_name = handler.__name__.replace("_ask_", "").replace("_", " ")
            question = (args.get("question") or "").strip()

            # Enqueue the original handler call for background execution
            from app.services.harness.run_service import (
                get_run_service,
                AGENT_RUN_TASK_TYPE,
            )
            from app.services import task_queue as _tq

            # Create a record for observability
            _svc = get_run_service()
            _svc.create_run_record(
                agent_name=agent_name,
                task=question,
                mode="queued",
                run_id=run_id,
                caller_context={
                    "org_id": ctx.get("org_id", "default-org"),
                    "app_id": ctx.get("app_id", "default-app"),
                    "user_id": user_id,
                },
            )
            _tq.enqueue(
                AGENT_RUN_TASK_TYPE,
                {
                    "run_id": run_id,
                    "agent_name": agent_name,
                    "task": question,
                    "handler": handler.__name__,
                },
            )
            return {
                "success": True,
                "run_id": run_id,
                "status": "queued",
                "agent": agent_name,
                "message": f"Task queued. Collect result with run_id={run_id}.",
            }

        return await handler(args, db, user_id, context)

    # Preserve the original name for introspection
    _wrapped.__name__ = handler.__name__
    return _wrapped


for _name, _schema, _handler, _desc in (
    (
        "ask_forecast_agent",
        _make_schema(
            "ask_forecast_agent",
            "Ask the EDIA Forecast Agent to discover forecastable products, run ensemble forecasts (ARIMA + XGBoost), retrieve cached results, audit backtest accuracy, or manage seasonal/causal business rules.",
            "Forecast Agent",
        ),
        _ask_forecast_agent,
        "Ask the EDIA Forecast Agent a forecasting question.",
    ),
    (
        "ask_report_agent",
        _make_schema(
            "ask_report_agent",
            "Ask the EDIA Report Agent to generate weekly forecast report cards (JSON), PPT slide decks, or persisted artifacts from the latest forecast data.",
            "Report Agent",
        ),
        _ask_report_agent,
        "Ask the EDIA Report Agent to generate a report or deck.",
    ),
    (
        "ask_perception",
        _make_schema(
            "ask_perception",
            "Ask the EDIA Perception Agent to build a live market snapshot: pull latest prices, compute 7-day changes, run anomaly detection, check historical patterns, and compute macro regime signals (crude/naphtha trend, market phase).",
            "Perception Agent",
        ),
        _ask_perception,
        "Ask the EDIA Perception Agent for a market snapshot.",
    ),
    (
        "ask_rag_research",
        _make_schema(
            "ask_rag_research",
            "Ask the EDIA RAG Research Agent to retrieve supporting context from the ChromaDB knowledge base: strategy docs, weekly report chunks, past pricing decisions, and stored news-event snippets.",
            "RAG Research Agent",
        ),
        _ask_rag_research,
        "Ask the EDIA RAG Research Agent for supporting context.",
    ),
    (
        "ask_diagnosis",
        _make_schema(
            "ask_diagnosis",
            "Ask the EDIA Diagnosis Agent to explain what is driving the market: traverse the product knowledge graph, build chain-impact explanations, score hypotheses, and produce a structured diagnosis (regime, drivers, evidence, contradictions).",
            "Diagnosis Agent",
        ),
        _ask_diagnosis,
        "Ask the EDIA Diagnosis Agent for a market diagnosis.",
    ),
    (
        "ask_pricing",
        _make_schema(
            "ask_pricing",
            "Ask the EDIA Pricing/Decision Agent to turn market understanding plus forecast into a pricing recommendation using the 9-step pricing ladder. Outputs recommended price, range, action, triggers, and watch signals.",
            "Pricing Agent",
        ),
        _ask_pricing,
        "Ask the EDIA Pricing Agent for a pricing recommendation.",
    ),
    (
        "ask_orchestrator",
        _make_schema(
            "ask_orchestrator",
            "Ask the EDIA Orchestrator Agent to handle a full market analysis request end-to-end: intent classification, context extraction, and routing through perception, RAG, diagnosis, forecast, pricing, and report sub-agents.",
            "Orchestrator Agent",
        ),
        _ask_orchestrator,
        "Ask the EDIA Orchestrator to run the full pipeline.",
    ),
    (
        "ask_intelligence",
        _make_schema(
            "ask_intelligence",
            "Ask the EDIA Intelligence Agent to retrieve market intelligence context: recent events, causal chain analyses, scenario probabilities, and auto-override decisions. Use this before pricing or diagnosis to inject event-aware context.",
            "Intelligence Agent",
        ),
        _ask_intelligence,
        "Ask the EDIA Intelligence Agent for market intelligence context.",
    ),
    (
        "ask_knowledge_graph",
        {
            "type": "function",
            "function": {
                "name": "ask_knowledge_graph",
                "description": "Query the EDIA Knowledge Graph to discover product relationships (upstream/downstream), substitution chains, and market dependencies across the 12 C5/C9 products. Use this for product-resolution and chain-impact questions.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "question": {
                            "type": "string",
                            "description": "Natural-language query (e.g. 'what are upstream products of isoprene?')",
                        },
                        "product": {
                            "type": "string",
                            "description": "Product name or ID to scope the query (e.g. 'isoprene', 'dcpd')",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["auto", "upstream", "downstream", "chain", "summary", "resolve"],
                            "description": "Query mode. Default: 'auto' (auto-detect from question)",
                        },
                        "async": {
                            "type": "boolean",
                            "description": "If true, enqueue for background execution. Default: false.",
                        },
                    },
                    "required": ["question"],
                },
            },
        },
        _ask_knowledge_graph,
        "Query the knowledge graph for product relationships.",
    ),
    (
        "ask_decision",
        {
            "type": "function",
            "function": {
                "name": "ask_decision",
                "description": "Ask the EDIA Decision Agent to produce a structured decision recommendation: evaluate pricing alternatives, apply critic gates, run the decision audit trail, and output a final decision with rationale, risks, and triggers.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product": {
                            "type": "string",
                            "description": "Product ID or name (e.g. 'isoprene', 'piperylene', 'dcpd', 'c5_resin', 'sis')",
                        },
                        "perception": {
                            "type": "object",
                            "description": "Optional perception snapshot dict from ask_perception",
                        },
                        "diagnosis": {
                            "type": "object",
                            "description": "Optional diagnosis dict from ask_diagnosis",
                        },
                        "forecast": {
                            "type": "object",
                            "description": "Optional forecast dict from forecast_brief",
                        },
                        "pricing_decision": {
                            "type": "object",
                            "description": "Optional pricing decision dict from ask_pricing",
                        },
                        "mode": {
                            "type": "string",
                            "enum": ["full", "dry_run"],
                            "description": "Mode: 'full' (default) or 'dry_run' (critic only)",
                        },
                        "async": {
                            "type": "boolean",
                            "description": "If true, enqueue for background execution. Default: false.",
                        },
                    },
                    "required": ["product"],
                },
            },
        },
        _ask_decision,
        "Ask the EDIA Decision Agent for a decision recommendation.",
    ),
    (
        "ask_macro_override",
        {
            "type": "function",
            "function": {
                "name": "ask_macro_override",
                "description": "Manage the active macro override (naphtha shock). Modes: get (read current), set (apply a ±4%-capped shock), clear (deactivate). Used to inject real-time macro events into the pricing ladder.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {
                            "type": "string",
                            "enum": ["get", "set", "clear"],
                            "description": "Operation: get current override, set new override, or clear. Default: 'get'",
                        },
                        "naphtha_shock_pct": {
                            "type": "number",
                            "description": "% shock to naphtha (e.g. 2.5 = +2.5%, capped at ±4%). Required when mode=set.",
                        },
                        "crude_shock_pct": {
                            "type": "number",
                            "description": "% crude shock for mode=set. Default: 0",
                        },
                        "event_type": {
                            "type": "string",
                            "enum": ["crude_spike", "tariff_shock", "force_majeure", "manual"],
                            "description": "Event type for mode=set. Default: manual",
                        },
                        "description": {
                            "type": "string",
                            "description": "Free-text description for mode=set",
                        },
                        "ttl_hours": {
                            "type": "integer",
                            "description": "Hours until auto-expiry for mode=set. Default: 24",
                        },
                        "async": {
                            "type": "boolean",
                            "description": "If true, enqueue for background execution. Default: false.",
                        },
                    },
                    "required": ["mode"],
                },
            },
        },
        _ask_macro_override,
        "Apply a macro-level pricing override.",
    ),
    (
        "ask_perception_intelligence_diagnosis",
        _make_schema(
            "ask_perception_intelligence_diagnosis",
            "BATCH TOOL: Ask the Perception, Intelligence, and Diagnosis agents in PARALLEL with a single call. Runs all three independent sub-agents concurrently (market snapshot, market intelligence context, and driver diagnosis) and returns a combined answer. Use this instead of calling ask_perception, ask_intelligence, and ask_diagnosis separately — it is 3x faster and uses one tool call instead of three.",
            "Perception + Intelligence + Diagnosis (parallel batch)",
        ),
        _ask_perception_intelligence_diagnosis,
        "Ask Perception, Intelligence, and Diagnosis agents in parallel.",
    ),
    (
        "ask_forecast_pricing",
        _make_schema(
            "ask_forecast_pricing",
            "BATCH TOOL: Ask the Forecast and Pricing agents in PARALLEL with a single call. Runs both sub-agents concurrently (ensemble forecast + 9-step pricing recommendation) and returns a combined answer. Use this instead of calling ask_forecast_agent and ask_pricing separately — it is ~2x faster and uses one tool call instead of two.",
            "Forecast + Pricing (parallel batch)",
        ),
        _ask_forecast_pricing,
        "Ask Forecast and Pricing agents in parallel.",
    ),
    (
        "ask_erp_kpi",
        {
            "type": "function",
            "function": {
                "name": "ask_erp_kpi",
                "description": "Query ERP KPIs (sales volume, revenue, margin, inventory) for a given product and period from the EDIA MySQL mirror. Use this for data-backed reporting and decision context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product": {
                            "type": "string",
                            "description": "Product ID or name (e.g. 'isoprene', 'piperylene', 'dcpd', 'c5_resin', 'sis')",
                        },
                        "days": {
                            "type": "integer",
                            "description": "Lookback period in days (default: 30)",
                        },
                        "async": {
                            "type": "boolean",
                            "description": "If true, enqueue for background execution. Default: false.",
                        },
                    },
                    "required": ["product"],
                },
            },
        },
        _ask_erp_kpi,
        "Query ERP KPIs from the EDIA data warehouse.",
    ),
    (
        "ask_erp_writeback",
        {
            "type": "function",
            "function": {
                "name": "ask_erp_writeback",
                "description": "Stage a pricing recommendation for ERP review. Human-in-the-loop: recommendations are 'pending' until admin approves. Records the writeback in the audit trail.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "product": {
                            "type": "string",
                            "description": "Product ID or name (e.g. 'isoprene', 'piperylene')",
                        },
                        "recommended_price": {
                            "type": "number",
                            "description": "Recommended price in CNY/ton",
                        },
                        "valid_from": {
                            "type": "string",
                            "description": "Start date (YYYY-MM-DD, default: today)",
                        },
                        "valid_until": {
                            "type": "string",
                            "description": "End date (YYYY-MM-DD, optional)",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "Free-text justification for the recommendation",
                        },
                        "async": {
                            "type": "boolean",
                            "description": "If true, enqueue for background execution. Default: false.",
                        },
                    },
                    "required": ["product", "recommended_price"],
                },
            },
        },
        _ask_erp_writeback,
        "Write a pricing decision back to ERP.",
    ),
):
    registry.register(
        name=_name,
        schema=_schema,
        handler=_maybe_async(_handler),
        category="delegation",
        enabled_by_default=False,
        description=_desc,
    )
