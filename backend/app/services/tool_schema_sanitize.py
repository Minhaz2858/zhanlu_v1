"""Tool-schema sanitizer for LLM providers with strict JSON-Schema validation.

P0 pilot fix (2026-10-31): several registered tools declare sub-schemas of
``{"type": "object"}`` WITHOUT a ``properties`` key (e.g. create_artifact's
``chart``, escalate_to_human's ``context``, forecast_rules' ``params``,
query_ontology's ``filters``). DashScope's OpenAI-compatible endpoint (and
other strict validators) reject the whole request with 400 "Check request
format" — the agent loop then dies before the first tool call. OpenAI and
DeepSeek tolerate the shape, so this only surfaced on the qwen path.

Fix: deep-copy the tool list and add ``"properties": {}`` to every object
schema that lacks one (valid JSON Schema everywhere; empty object = any
object value, preserving the original intent).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _patch(node: Any) -> Any:
    if isinstance(node, dict):
        node = {k: _patch(v) for k, v in node.items()}
        if node.get("type") == "object" and "properties" not in node:
            node = {**node, "properties": {}}
        return node
    if isinstance(node, list):
        return [_patch(v) for v in node]
    return node


def _wrap_flat(tool: Any) -> Any:
    """Wrap a flat-form tool ({name, description, parameters|properties})
    into the OpenAI {type: function, function: {...}} envelope, preserving
    its parameter schema."""
    if (
        isinstance(tool, dict)
        and "function" not in tool
        and tool.get("name")
    ):
        params = tool.get("parameters")
        if not isinstance(params, dict):
            params = tool.get("input_schema")
        if not isinstance(params, dict):
            props = tool.get("properties")
            params = {"type": "object", "properties": props if isinstance(props, dict) else {}}
        elif "type" not in params:
            params = {**params, "type": "object"}
        return {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool.get("description") or "",
                "parameters": params,
            },
        }
    return tool


def sanitize_tool_schemas(tools: list[dict] | None) -> list[dict] | None:
    """Return provider-safe tool schemas (or None unchanged). Never raises.

    Two fixes applied per tool:
    1. Nested bare ``{"type": "object"}`` schemas get ``"properties": {}``
       (strict validators 400 otherwise).
    2. Flat-form tools are wrapped in the OpenAI envelope — DashScope 400s
       with "'function' is a required property" for unwrapped tools
       (live error: tools.6 / tools.7).
    """
    if not tools:
        return tools
    try:
        return [_wrap_flat(_patch(t)) for t in tools]
    except Exception as exc:  # noqa: BLE001 — sanitizing must never break a call
        logger.warning("tool_schema_sanitize: passthrough on error: %s", exc)
        return tools


__all__ = ["sanitize_tool_schemas"]
