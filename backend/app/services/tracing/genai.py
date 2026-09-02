"""GenAI semantic-convention helpers (OpenTelemetry GenAI conventions).

A focused subset of the OTel GenAI semantic conventions:
https://opentelemetry.io/docs/specs/semconv/gen-ai/

Attributes are plain dicts so any TracingProvider can carry them; the OTel
provider maps them onto real span attributes.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# ── Attribute keys (semconv) ────────────────────────────────────────────
GEN_AI_OPERATION_NAME = "gen_ai.operation.name"
GEN_AI_SYSTEM = "gen_ai.system"
GEN_AI_REQUEST_MODEL = "gen_ai.request.model"
GEN_AI_REQUEST_TEMPERATURE = "gen_ai.request.temperature"
GEN_AI_USAGE_INPUT_TOKENS = "gen_ai.usage.input_tokens"
GEN_AI_USAGE_OUTPUT_TOKENS = "gen_ai.usage.output_tokens"
GEN_AI_TOOL_NAME = "gen_ai.tool.name"
GEN_AI_TOOL_ARGS_HASH = "gen_ai.tool.args_hash"
GEN_AI_TOOL_ARGS_PREVIEW = "gen_ai.tool.args_preview"
GEN_AI_TOOL_SUCCESS = "gen_ai.tool.success"


def llm_attributes(
    model: str,
    temperature: float | None = None,
    operation: str = "chat",
) -> dict[str, Any]:
    """Attributes describing an LLM completion request."""
    attrs: dict[str, Any] = {
        GEN_AI_OPERATION_NAME: operation,
        GEN_AI_SYSTEM: "zhanlu",
        GEN_AI_REQUEST_MODEL: model or "",
    }
    if temperature is not None:
        attrs[GEN_AI_REQUEST_TEMPERATURE] = temperature
    return attrs


def usage_attributes(prompt_tokens: int, completion_tokens: int) -> dict[str, Any]:
    """Token-usage attributes recorded after a completion."""
    return {
        GEN_AI_USAGE_INPUT_TOKENS: prompt_tokens or 0,
        GEN_AI_USAGE_OUTPUT_TOKENS: completion_tokens or 0,
    }


def tool_attributes(tool_name: str, arguments: dict | None = None) -> dict[str, Any]:
    """Attributes describing a tool invocation.

    Never includes raw argument VALUES unless TRACE_INCLUDE_TOOL_INPUT is
    enabled — credentials and row data must not leak into traces by default.
    """
    attrs: dict[str, Any] = {GEN_AI_TOOL_NAME: tool_name}
    try:
        blob = json.dumps(arguments or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = ""
    attrs[GEN_AI_TOOL_ARGS_HASH] = hashlib.sha256(blob.encode()).hexdigest()[:16]
    from app.config import settings

    if getattr(settings, "TRACE_INCLUDE_TOOL_INPUT", False):
        attrs[GEN_AI_TOOL_ARGS_PREVIEW] = blob[:500]
    return attrs


def error_text(exc: BaseException) -> str:
    """Truncated, single-line error text for span attributes."""
    return str(exc).replace("\n", " ")[:500]


__all__ = [
    "GEN_AI_OPERATION_NAME",
    "GEN_AI_SYSTEM",
    "GEN_AI_REQUEST_MODEL",
    "GEN_AI_REQUEST_TEMPERATURE",
    "GEN_AI_USAGE_INPUT_TOKENS",
    "GEN_AI_USAGE_OUTPUT_TOKENS",
    "GEN_AI_TOOL_NAME",
    "GEN_AI_TOOL_ARGS_HASH",
    "GEN_AI_TOOL_ARGS_PREVIEW",
    "GEN_AI_TOOL_SUCCESS",
    "llm_attributes",
    "usage_attributes",
    "tool_attributes",
    "error_text",
]
