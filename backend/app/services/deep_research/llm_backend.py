"""Real LLM + tool wiring for the deep-research protocol.

All model traffic stays on the Synexia routing path (``llm_service.call_llm``
with an optional pinned ``LLMEndpoint``); tools run through the same
``agent_tools.execute_tool`` dispatcher the agent loop uses, so RBAC and
binding isolation apply unchanged. Builders return async callables matching
the orchestrator's injectable interfaces.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

from app.services.agent_tools import execute_tool
from app.services.llm_service import call_llm

logger = logging.getLogger(__name__)

# Tool families per scope (mirrors orchestrator.SCOPE_TOOLS). Injected into
# the planner system prompt so the model emits only real, in-scope tools.
_SCOPE_WEB_TOOLS = ("web_search", "web_extract")
_SCOPE_INTERNAL_TOOLS = ("sql_query", "ask_data_agent", "comprehensive_data")
_SCOPE_TOOL_NAMES = {
    "web": _SCOPE_WEB_TOOLS,
    "internal": _SCOPE_INTERNAL_TOOLS,
    "both": tuple(sorted(set(_SCOPE_WEB_TOOLS) | set(_SCOPE_INTERNAL_TOOLS))),
}


def _planner_system_prompt(scope: str) -> str:
    """Planner system prompt: names the REAL tools the run may invoke.

    The pre-fix planner saw only the raw question, so deepseek defaulted to
    web_search for every step and internal questions (product codes, sales,
    forecasts) never reached the bound data warehouse. The prompt also
    enforces hybrid coverage for scope=``both``: a plan that only web-searches
    a data question is invalid.
    """
    tool_list = ", ".join(_SCOPE_TOOL_NAMES.get(scope, _SCOPE_TOOL_NAMES["both"]))
    if scope == "web":
        scope_rule = (
            "The user selected WEB scope: use ONLY web tools "
            "(web_search / web_extract) for public/industry information."
        )
    elif scope == "internal":
        scope_rule = (
            "The user selected INTERNAL DATA scope: use ONLY the bound "
            "warehouse tools (sql_query / ask_data_agent / "
            "comprehensive_data). Do NOT call web tools."
        )
    else:
        scope_rule = (
            "The user selected BOTH scopes — hybrid research is REQUIRED. "
            "Split the plan across BOTH families:\n"
            "  • Internal-data steps (ask_data_agent / sql_query / "
            "comprehensive_data) for facts that live in the bound "
            "warehouse: product codes (e.g. C5C9), sales, forecasts, "
            "quantities, revenue, inventory.\n"
            "  • Web steps (web_search / web_extract) for market context, "
            "industry pricing, public benchmarks.\n"
            "A plan that searches only ONE family is invalid — when the "
            "question names a product code or metric, ALWAYS include an "
            "internal-data step."
        )
    return (
        "You are the research planner for a deep-research run. Decompose the "
        "research question into concrete gather steps.\n\n"
        f"Scope: {scope}\n"
        f"Tools available in this run: {tool_list}\n\n"
        f"{scope_rule}\n\n"
        "Rules:\n"
        "- Each step MUST name one or more tools from the available list "
        "above. Never invent tools.\n"
        "- Steps run independently; make each question self-contained.\n"
        "- Prefer 3-6 focused steps over one giant step.\n"
        "Return JSON matching the schema: {\"steps\": [{\"id\", "
        "\"question\", \"tools\", \"rationale\"}]}."
    )

PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "question": {"type": "string"},
                    "tools": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"},
                },
                "required": ["question", "tools"],
            },
        }
    },
}


def build_planner(*, endpoint=None, timeout_s: int = 90, scope: str = "both"):
    """Async callable returning a research-plan dict (``{"steps": [...]}``).

    ``scope`` is injected into the planner system prompt so the model KNOWS
    which tools exist and — for scope=``both`` — that the plan MUST mix
    internal-data steps (ask_data_agent / sql_query) with web steps. Before
    this fix the planner received ONLY the raw question (no tool universe,
    no scope semantics), so it defaulted to web_search for every step and a
    C5C9-style product question never touched the bound warehouse.

    Tolerates JSON-schema miss: falls back to parsing ``response`` as JSON,
    then to ``{}`` (the orchestrator's fallback step covers the empty case).
    """
    _scope = scope if scope in ("web", "internal", "both") else "both"

    async def planner(messages: list[dict]) -> dict:
        try:
            resp = await asyncio.wait_for(
                call_llm(
                    messages=[{"role": "system", "content": _planner_system_prompt(_scope)}]
                    + list(messages),
                    temperature=0.2,
                    response_json_schema=PLAN_SCHEMA,
                    endpoint=endpoint,
                ),
                timeout=timeout_s,
            )
        except Exception as e:
            logger.warning("deep research planner LLM error (non-fatal): %s", e)
            return {}
        data = (resp or {}).get("data")
        if isinstance(data, dict):
            return data
        raw = (resp or {}).get("response") or ""
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}

    return planner


def build_tool_executor(*, db, user_id: Optional[str] = None, context: Optional[dict] = None):
    """Async callable running a named tool through the agent-loop dispatcher."""
    async def tool_executor(name: str, args: dict) -> dict:
        return await execute_tool(
            name, args, db, user_id=user_id, context=context
        )

    return tool_executor


def _build_text_llm(*, endpoint=None, timeout_s: int, temperature: float):
    async def text_llm(messages: list[dict]) -> str:
        resp = await asyncio.wait_for(
            call_llm(messages=messages, temperature=temperature, endpoint=endpoint),
            timeout=timeout_s,
        )
        return (resp or {}).get("response") or ""

    return text_llm


def build_synthesizer(*, endpoint=None, timeout_s: int = 90, temperature: float = 0.4):
    """Async callable returning the final cited report markdown."""
    return _build_text_llm(endpoint=endpoint, timeout_s=timeout_s, temperature=temperature)


def build_critic(*, endpoint=None, timeout_s: int = 90, temperature: float = 0.2):
    """Async callable returning a gap statement for the next gather round."""
    return _build_text_llm(endpoint=endpoint, timeout_s=timeout_s, temperature=temperature)
