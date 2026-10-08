"""ReAct text-scratchpad fallback (IBM ReAct article, gap #2).

For models/providers WITHOUT reliable native function calling, the classic
ReAct prompting pattern expresses tool use as plain text::

    Thought: I should search for X
    Action: web_search
    Action Input: {"query": "X"}

This parser converts such text into a synthetic native tool call so the
governed ReAct loop can execute it. Enabled per-deployment via
``REACT_SCRATCHPAD_FALLBACK_ENABLED`` (default False — native tool calling
is the primary path; this is graceful degradation, not a replacement).

Safety: the parsed action name MUST exist in ``allowed_tools`` (when
provided) — free-text actions are never executed.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from typing import Any, Optional

logger = logging.getLogger(__name__)

_ACTION_RE = re.compile(
    r"^\s*Action\s*[：:]\s*(\S[^\n]*)$", re.MULTILINE,
)
_ACTION_INPUT_RE = re.compile(
    r"^\s*Action\s*Input\s*[：:]\s*(.+?)(?=^\s*(?:Thought|Action|Observation|Final Answer)\s*[：:]|\Z)",
    re.MULTILINE | re.DOTALL,
)


def parse_react_text(
    content: str,
    allowed_tools: Optional[list[str]] = None,
) -> Optional[dict[str, Any]]:
    """Parse classic ReAct scratchpad text into a synthetic tool call.

    Returns an OpenAI-format tool_calls entry (``{"id", "type": "function",
    "function": {"name", "arguments"}}``) or None when the text does not
    express a parseable action.
    """
    if not content or ("Action:" not in content and "Action：" not in content):
        return None

    action_match = _ACTION_RE.search(content)
    if not action_match:
        return None
    action_name = action_match.group(1).strip().strip("`'\"")
    if not action_name:
        return None
    if allowed_tools is not None and action_name not in allowed_tools:
        logger.info(
            "react_fallback: action %r not in allowed tools — ignoring",
            action_name,
        )
        return None

    input_match = _ACTION_INPUT_RE.search(content)
    raw_input = (input_match.group(1) if input_match else "").strip()
    arguments: Any = {}
    if raw_input:
        # Try JSON first; fall back to a bare string under "input".
        try:
            arguments = json.loads(raw_input)
            if not isinstance(arguments, dict):
                arguments = {"input": arguments}
        except json.JSONDecodeError:
            arguments = {"input": raw_input}

    logger.info("react_fallback: parsed scratchpad action %r", action_name)
    return {
        "id": f"react-{uuid.uuid4().hex[:8]}",
        "type": "function",
        "function": {
            "name": action_name,
            "arguments": json.dumps(arguments, ensure_ascii=False),
        },
    }


__all__ = ["parse_react_text"]
