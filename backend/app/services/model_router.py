"""Task-based model routing.

Routes LLM calls to the most appropriate model based on task type.
The routing table is configured via ``MODEL_TASK_ROUTING`` (JSON mapping
of task_type → model_name). Falls back to ``settings.LLM_MODEL`` when
no routing rule matches.

Task types and their recommended models:
- **simple_chat**: general Q&A, small models (deepseek-chat, gpt-4o-mini)
- **tool_use**: function-calling workloads, larger context models
- **code_gen**: code generation, models fine-tuned for code
- **document_gen**: long-form generation, max-tokens models
- **reasoning**: chain-of-thought, reasoning models (deepseek-reasoner, o1)
- **embedding**: embedding models (never routed via this module)
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Default routing: most models map to the same LLM_MODEL.
# Override via MODEL_TASK_ROUTING in config.
_DEFAULT_ROUTING: dict[str, str] = {}


def _load_routing_table() -> dict[str, str]:
    """Load the task→model routing table from settings."""
    from app.config import settings

    raw = getattr(settings, "MODEL_TASK_ROUTING", "") or ""
    if not raw or not raw.strip():
        return dict(_DEFAULT_ROUTING)

    try:
        routing = json.loads(raw)
        if not isinstance(routing, dict):
            logger.warning("MODEL_TASK_ROUTING is not a JSON object — ignoring")
            return dict(_DEFAULT_ROUTING)
        # Merge with defaults (settings override)
        merged = dict(_DEFAULT_ROUTING)
        merged.update(routing)
        return merged
    except (json.JSONDecodeError, TypeError) as e:
        logger.warning("MODEL_TASK_ROUTING parse failed (non-fatal): %s", e)
        return dict(_DEFAULT_ROUTING)


def route_model(task_type: str) -> str:
    """Return the model name for the given task type.

    Args:
        task_type: One of ``simple_chat``, ``tool_use``, ``code_gen``,
                   ``document_gen``, ``reasoning``.

    Returns:
        The model name to use, falling back to ``settings.LLM_MODEL``.
    """
    from app.config import settings

    routing = _load_routing_table()
    model = routing.get(task_type)
    if model:
        return model
    return settings.LLM_MODEL


def classify_task(messages: list[dict], tools_specified: bool = False) -> str:
    """Heuristically classify the task type from the conversation context.

    This is a fast heuristic; for high-confidence classification, use
    ``classify_task_with_llm()``.

    Returns one of: ``simple_chat``, ``reasoning``, ``tool_use``,
    ``code_gen``, ``document_gen``.
    """
    if tools_specified:
        return "tool_use"

    # Collect all user messages
    user_texts: list[str] = []
    for m in messages:
        if m.get("role") == "user" and isinstance(m.get("content"), str):
            user_texts.append(m["content"].lower())

    combined = " ".join(user_texts)

    # Code-gen signals
    code_keywords = [
        "write code", "implement function", "create a python", "write a script",
        "debug this", "fix this code", "refactor", "generate code", "code for",
        "编写代码", "写一个函数", "修复代码", "重构",
    ]
    if any(kw in combined for kw in code_keywords):
        return "code_gen"

    # Reasoning signals
    reason_keywords = [
        "think step by step", "reason about", "analyze", "explain why",
        "prove", "logical", "mathematical", "calculate", "推导", "推理",
    ]
    if any(kw in combined for kw in reason_keywords):
        return "reasoning"

    # Document generation signals (long messages requesting creation)
    doc_keywords = [
        "write a report", "generate a document", "write an article",
        "write a blog", "create a summary", "撰写报告", "写一篇文章",
    ]
    if any(kw in combined for kw in doc_keywords):
        return "document_gen"

    return "simple_chat"


def get_model_for_request(
    messages: list[dict],
    tools_specified: bool = False,
    explicit_task: Optional[str] = None,
) -> str:
    """One-stop routing: classify the task and return the right model.

    Args:
        messages: The chat messages list.
        tools_specified: Whether tool calling is expected.
        explicit_task: Override the heuristic with an explicit task type.

    Returns:
        The model name to use.
    """
    task = explicit_task or classify_task(messages, tools_specified)
    model = route_model(task)
    logger.debug("Model routing: task=%s → model=%s", task, model)
    return model


__all__ = [
    "route_model",
    "classify_task",
    "get_model_for_request",
]
