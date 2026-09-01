"""Tool result classification -- distinguishes side-effect-free from mutating tools.

Powers three capabilities:

1. **Interrupt recovery**: when a turn is interrupted (approval pause, error),
   side-effect-free tool calls can be safely discarded -- the LLM can re-run
   them. Mutating tool calls must be preserved because the world has changed.

2. **Checkpoint decisions**: only persist checkpoints after mutating tools
   have landed, not after read-only explorations.

3. **Guardrail accuracy**: the loop guardrail uses this to decide whether
   repeated identical calls are "no-progress" (read-only) or legitimate
   (mutations may produce different results each time).

Inspired by Hermes' ``agent/tool_result_classification.py``.
"""
from __future__ import annotations

import json
from typing import Any


# Tools that modify files on disk. Their results prove the write landed.
FILE_MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    "write_file",
})

# Tools that have NO side effects -- safe to discard on interrupt, safe to
# treat repeated identical results as "no progress".
NO_EFFECT_TOOL_NAMES: frozenset[str] = frozenset({
    "read_file", "web_search", "web_extract", "list_tools",
    "list_market_agents", "list_knowledge_bases", "search_skills",
    "skills", "skills_hub", "session_search", "interrupt",
})

# Tools that mutate state but aren't file writes (agents, skills, automations,
# code execution, memory). They have side effects but we can't verify "landed"
# the same way we can for file writes.
STATE_MUTATING_TOOL_NAMES: frozenset[str] = frozenset({
    "create_agent", "update_agent", "create_skill", "update_skill",
    "create_automation", "update_automation", "execute_code",
    "ask_data_agent", "memory",
})


def tool_may_have_side_effect(tool_name: str) -> bool:
    """Return True if the tool may have side effects (mutations, state changes).

    Read-only tools (read_file, web_search, etc.) return False. All others
    return True. Used for interrupt recovery: side-effect-free tool calls
    can be safely discarded and re-run.
    """
    return tool_name not in NO_EFFECT_TOOL_NAMES


def is_file_mutating_tool(tool_name: str) -> bool:
    """Return True if the tool writes files on disk."""
    return tool_name in FILE_MUTATING_TOOL_NAMES


def file_mutation_result_landed(tool_name: str, result: Any) -> bool:
    """Return True when a file mutation result proves the write landed.

    For ``write_file``, checks that ``result.success`` is True. For other
    tools, returns False (they're not file mutations).

    Args:
        tool_name: The tool that was called.
        result: The tool result (dict or JSON string).

    Returns:
        True if this is a file mutation that succeeded.
    """
    if tool_name not in FILE_MUTATING_TOOL_NAMES:
        return False

    # Parse result if it's a string
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return False

    if not isinstance(result, dict):
        return False

    return result.get("success") is True


def is_safe_to_discard_on_interrupt(tool_name: str) -> bool:
    """Return True if discarding this tool's result on interrupt is safe.

    Side-effect-free tools (read_file, web_search) are safe to discard --
    the LLM can re-run them. Mutating tools must be preserved.
    """
    return not tool_may_have_side_effect(tool_name)


__all__ = [
    "FILE_MUTATING_TOOL_NAMES",
    "NO_EFFECT_TOOL_NAMES",
    "STATE_MUTATING_TOOL_NAMES",
    "tool_may_have_side_effect",
    "is_file_mutating_tool",
    "file_mutation_result_landed",
    "is_safe_to_discard_on_interrupt",
]
