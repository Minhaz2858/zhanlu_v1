"""think tool — explicit ReAct "Thought" step (IBM ReAct article, gap #1).

Reasoning models expose their chain of thought natively (reasoning_content).
Standard models jump straight to tool calls, skipping the verbalized
decomposition that the ReAct paper shows improves multi-step task outcomes.
The ``think`` tool is a no-op that lets the model externalize a planning
thought mid-loop: cheap (no side effects), visible in the reasoning trace,
and it structures the scratchpad.
"""

from __future__ import annotations

import logging

from app.services.tool_registry import registry

logger = logging.getLogger(__name__)

_THOUGHTS: dict[str, list[str]] = {}
_MAX_THOUGHTS_PER_CONV = 20


async def _think(
    args: dict,
    db,
    user_id: str | None,
    context: dict | None = None,
) -> dict:
    """Record the model's reasoning thought. No side effects."""
    thought = (args.get("thought") or "").strip()
    if not thought:
        return {"success": False, "error": "thought is required"}

    conv_id = (context or {}).get("conversation_id") or "global"
    bucket = _THOUGHTS.setdefault(conv_id, [])
    if len(bucket) >= _MAX_THOUGHTS_PER_CONV:
        bucket.pop(0)
    bucket.append(thought[:2000])

    logger.debug("think [%s]: %s", conv_id, thought[:120])
    return {
        "success": True,
        "recorded": True,
        "hint": (
            "Thought recorded. Use it to decide your next action or, if the "
            "task is complete, write the final answer now."
        ),
    }


THINK_SCHEMA = {
    "type": "function",
    "function": {
        "name": "think",
        "description": (
            "Externalize a planning thought (ReAct 'Thought' step). Use "
            "before acting on complex, multi-step requests to decompose the "
            "task, weigh options, or reassess after an observation. Pure "
            "reasoning — no side effects, returns nothing to act on."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "thought": {
                    "type": "string",
                    "description": (
                        "Your current reasoning: the subtask you identify, "
                        "the option you're weighing, or your assessment of "
                        "the last observation."
                    ),
                },
            },
            "required": ["thought"],
        },
    },
}

registry.register(
    name="think",
    schema=THINK_SCHEMA,
    handler=_think,
    category="reasoning",
    enabled_by_default=True,
    description="No-op ReAct thought step — externalize planning reasoning.",
)

__all__ = ["_think", "THINK_SCHEMA"]
