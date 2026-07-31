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
    """
    try:
        import jsonschema
    except ImportError:
        logger.debug("validate_tool_args: jsonschema not installed; skipping")
        return None
    try:
        jsonschema.validate(args, schema)
        return None
    except jsonschema.ValidationError as e:
        # e.message is concise: "X is a required property" / "Y is not of type Z"
        return e.message
    except Exception as e:
        logger.debug("validate_tool_args: validator error (treating as pass): %s", e)
        return None
