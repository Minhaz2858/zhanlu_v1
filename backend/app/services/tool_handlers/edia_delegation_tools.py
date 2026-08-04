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
from typing import Any

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
    "ask_weekly_report", "ask_erp_kpi", "ask_erp_writeback",
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
    max_iterations: int = 8,
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


async def _ask_weekly_report(args: dict, db: Session, user_id: str | None, context: dict | None = None) -> dict:
    """Weekly report generation via direct Python service (no sub-agent delegation)."""
    from app.services.weekly_report.tool import _handle_ask_weekly_report
    return await _handle_ask_weekly_report(args, db, user_id, context)


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
                },
                "required": ["question"],
            },
        },
    }


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
        _make_schema(
            "ask_knowledge_graph",
            "Query the EDIA Knowledge Graph to discover product relationships (upstream/downstream), substitution chains, and market dependencies across the 12 C5/C9 products. Use this for product-resolution and chain-impact questions.",
            "Knowledge Graph Agent",
        ),
        _ask_knowledge_graph,
        "Query the knowledge graph for product relationships.",
    ),
    (
        "ask_decision",
        _make_schema(
            "ask_decision",
            "Ask the EDIA Decision Agent to produce a structured decision recommendation: evaluate pricing alternatives, apply critic gates, run the decision audit trail, and output a final decision with rationale, risks, and triggers.",
            "Decision Agent",
        ),
        _ask_decision,
        "Ask the EDIA Decision Agent for a decision recommendation.",
    ),
    (
        "ask_macro_override",
        _make_schema(
            "ask_macro_override",
            "Apply a macro-level override to any pricing recommendation — e.g. 'add 5% for naphtha due to crude disruption' or 'cap at ceiling for styrene'. Registers the override in the audit trail.",
            "Macro Override",
        ),
        _ask_macro_override,
        "Apply a macro-level pricing override.",
    ),
    (
        "ask_weekly_report",
        _make_schema(
            "ask_weekly_report",
            "Ask the EDIA Weekly Report Agent to generate a complete weekly report for a product or portfolio: pull data, compute charts, generate narrative, and produce a formatted report (JSON/PDF/Word/PPT).",
            "Weekly Report Agent",
        ),
        _ask_weekly_report,
        "Ask the EDIA Weekly Report Agent to generate a report.",
    ),
    (
        "ask_erp_kpi",
        _make_schema(
            "ask_erp_kpi",
            "Query ERP KPIs (sales volume, revenue, margin, inventory) for a given product and period from the EDIA MySQL mirror. Use this for data-backed reporting and decision context.",
            "ERP KPI Agent",
        ),
        _ask_erp_kpi,
        "Query ERP KPIs from the EDIA data warehouse.",
    ),
    (
        "ask_erp_writeback",
        _make_schema(
            "ask_erp_writeback",
            "Write a confirmed pricing decision or override back to the ERP system. Requires a decision_id or explicit pricing parameters. Records the writeback in the audit trail.",
            "ERP Writeback Agent",
        ),
        _ask_erp_writeback,
        "Write a pricing decision back to ERP.",
    ),
):
    registry.register(
        name=_name,
        schema=_schema,
        handler=_handler,
        category="delegation",
        enabled_by_default=False,
        description=_desc,
    )
