"""Upfront tool-argument validation.

Validates the LLM-produced ``arguments`` dict against the tool's declared
OpenAI JSON-schema BEFORE the handler is invoked. This catches malformed
calls (missing required fields, wrong types) immediately — without burning
a handler round-trip or an LLM reformulation — and returns a structured
``permanent`` failure so the agent can correct itself.

Schema sources (checked in order):
  1. ``registry.get_entry(name).schema["function"]["parameters"]``  (registry tools)
  2. ``_get_all_crud_schemas()[name]["function"]["parameters"]``    (CRUD tools)

When no schema is found for a tool, validation is skipped (graceful — never
block a tool on a missing schema). ``jsonschema`` is an existing project
dependency (used by skills_loader).
"""
from __future__ import annotations

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def get_tool_schema(tool_name: str) -> Optional[dict]:
    """Return the OpenAI args JSON-schema for a tool, or None if not found.

    Checks the ToolRegistry first, then the CRUD schema map.
    """
    # 1. Registry tools
    try:
        from app.services.tool_registry import registry
        entry = registry.get_entry(tool_name)
        if entry is not None and entry.schema:
            params = entry.schema.get("function", {}).get("parameters")
            if isinstance(params, dict):
                return params
    except Exception as e:
        logger.debug("get_tool_schema: registry lookup failed for '%s': %s", tool_name, e)

    # 2. CRUD tools
    try:
        from app.services.agent_prompts import _get_all_crud_schemas
        schemas = _get_all_crud_schemas()
        s = schemas.get(tool_name)
        if s:
            params = s.get("function", {}).get("parameters")
            if isinstance(params, dict):
                return params
    except Exception as e:
        logger.debug("get_tool_schema: CRUD lookup failed for '%s': %s", tool_name, e)

    return None


def validate_tool_args(args: dict, schema: dict) -> Optional[str]:
    """Validate ``args`` against a JSON schema.

    Returns ``None`` on pass, or a human-readable error description on failure.
    Never raises — a validator/infra problem is treated as a pass (don't block).

    Numeric-string coercion (2026-11-02): LLMs frequently serialize
    integers/floats as strings (``"sketch_index": "0"``). Under strict JSON
    typing that is rejected as ``"'0' is not of type 'integer'"`` — and the
    turn then burns reflexion round-trips (each ~1-2 min with tool execution)
    retrying the same unambiguous serialization mistake (observed live on
    fusion360_extrude with qwen3.5-omni-plus, plus a downstream 429). When the
    schema says ``integer``/``number`` and the value is a plain numeric
    string, it is coerced IN PLACE so both the validator and the handler see
    the intended type. Anything ambiguous (``"nan"``, ``"20mm"``) is left
    strictly untouched — this is a serialization fix, never a value guess.
    """
    try:
        import jsonschema
    except ImportError:
        logger.debug("validate_tool_args: jsonschema not installed; skipping")
        return None
    _coerce_numeric_strings(args, schema)
    try:
        jsonschema.validate(args, schema)
        return None
    except jsonschema.ValidationError as e:
        # e.message is concise: "X is a required property" / "Y is not of type Z"
        return e.message
    except Exception as e:
        logger.debug("validate_tool_args: validator error (treating as pass): %s", e)
        return None


_INT_RE = re.compile(r"[+-]?\d+")
_NUM_RE = re.compile(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def _coerce_numeric_strings(args: dict, schema: dict) -> None:
    """In-place: coerce plain numeric strings to int/float per declared type."""
    if not isinstance(args, dict) or not isinstance(schema, dict):
        return
    props = schema.get("properties")
    if not isinstance(props, dict):
        return
    for key, spec in props.items():
        if key not in args or not isinstance(spec, dict):
            continue
        val = args[key]
        if not isinstance(val, str):
            continue
        declared = spec.get("type")
        if isinstance(declared, str):
            declared = [declared]
        if not isinstance(declared, list):
            continue
        s = val.strip()
        # Only coerce when string is NOT already an accepted type — a union
        # like ["number", "string"] accepts the string as-is (no rewrite).
        if "string" in declared:
            continue
        if "integer" in declared and _INT_RE.fullmatch(s):
            args[key] = int(s)
        elif "number" in declared and _NUM_RE.fullmatch(s):
            args[key] = float(s)
