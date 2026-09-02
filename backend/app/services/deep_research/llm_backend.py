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


def build_planner(*, endpoint=None, timeout_s: int = 90):
    """Async callable returning a research-plan dict (``{"steps": [...]}``).

    Tolerates JSON-schema miss: falls back to parsing ``response`` as JSON,
    then to ``{}`` (the orchestrator's fallback step covers the empty case).
    """
    async def planner(messages: list[dict]) -> dict:
        try:
            resp = await asyncio.wait_for(
                call_llm(
                    messages=messages,
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
