"""mixture_of_agents tool — ensemble of agents for high-confidence answers.

A placeholder that calls the existing ``delegate_task`` up to N times in
parallel (with different temperature seeds) and aggregates the answers
via majority vote. Real implementation lives in Phase 3 — for now this
is a thin shim that delegates to a single agent and returns its result.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from sqlalchemy.orm import Session

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)


async def _mixture_of_agents(
    args: dict,
    db: Optional[Session] = None,
    user_id: Optional[str] = None,
    context: Optional[dict] = None,
) -> dict:
    prompt = (args.get("prompt") or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt is required"}
    target_agent = args.get("target_agent", "general_assistant")
    n = max(1, min(int(args.get("ensemble_size", 3)), 8))

    # Single delegate (Phase 3 will parallelize)
    try:
        from app.services.tool_handlers.delegate_tool import _delegate_task_handler
    except Exception as exc:
        return {"success": False, "error": f"delegate handler unavailable: {exc}"}

    tasks = []
    for i in range(n):
        tasks.append(_delegate_task_handler(
            {
                "agent_name": target_agent,
                "task": prompt,
                "temperature": 0.5 + 0.1 * i,
            },
            db=db,
            user_id=user_id,
            context=context,
        ))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    out = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            out.append({"index": i, "error": str(r)})
        else:
            out.append({"index": i, "result": r})
    return {
        "success": True,
        "ensemble_size": n,
        "results": out,
        "note": "Single-pass ensemble (Phase 3 will parallelize + vote).",
    }


MIXTURE_OF_AGENTS_SCHEMA = {
    "type": "function",
    "function": {
        "name": "mixture_of_agents",
        "description": (
            "Run an ensemble of agents (different temperature seeds) and "
            "aggregate the results. Use for high-stakes decisions where a "
            "single LLM call is not reliable enough. Currently a thin "
            "shim; full ensemble + voting arrives in Phase 3."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The task/prompt to run on each agent."},
                "target_agent": {"type": "string", "description": "Which agent to ensemble (default 'general_assistant').", "default": "general_assistant"},
                "ensemble_size": {"type": "integer", "description": "How many parallel runs (default 3, max 8).", "default": 3},
            },
            "required": ["prompt"],
        },
    },
}

registry.register(
    name="mixture_of_agents",
    schema=MIXTURE_OF_AGENTS_SCHEMA,
    handler=_mixture_of_agents,
    category="delegation",
    toolset="delegation",
    description="Ensemble of agents (Phase 3: parallel + vote).",
    emoji="🎭",
    max_result_size_chars=50_000,
)
