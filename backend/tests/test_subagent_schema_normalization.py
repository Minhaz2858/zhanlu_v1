"""Tests for sub-agent tool schema normalization (P1, 2026-08-29).

Sub-agents were crashing with DeepSeek 400 ``tools[N].type: unknown variant
'object'`` because the ``universal_*`` data tools register bare parameters
dicts and ``collect_enterprise_data`` a flat form — neither is an OpenAI
function envelope. The normalizer wraps them.
"""

from __future__ import annotations

from app.services.tool_handlers.delegate_tool import _normalize_subagent_schema


def test_wrapped_schema_passthrough():
    schema = {"type": "function", "function": {"name": "x", "parameters": {}}}
    assert _normalize_subagent_schema("x", schema) is schema


def test_bare_parameters_dict_wrapped():
    schema = {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]}
    out = _normalize_subagent_schema("universal_query", schema, "Query the DB")
    assert out["type"] == "function"
    assert out["function"]["name"] == "universal_query"
    assert out["function"]["parameters"] is schema


def test_flat_form_wrapped():
    schema = {"name": "collect_enterprise_data", "description": "desc", "parameters": {"type": "object"}}
    out = _normalize_subagent_schema("collect_enterprise_data", schema)
    assert out["type"] == "function"
    assert out["function"]["name"] == "collect_enterprise_data"
    assert out["function"]["description"] == "desc"


def test_non_dict_passthrough():
    assert _normalize_subagent_schema("x", None) is None


def test_registry_has_no_unwrapped_enabled_schemas():
    """Guards the actual registry: every enabled_by_default entry must
    normalize to an envelope (this is the bug that killed sub-agents)."""
    from app.services import tool_handlers  # noqa: F401
    from app.services.tool_registry import registry

    for name in registry.list_available():
        entry = registry.get_entry(name)
        if entry and entry.enabled_by_default:
            out = _normalize_subagent_schema(name, entry.schema, entry.description or "")
            assert out["type"] == "function", f"{name} did not normalize"
            assert "function" in out, f"{name} missing envelope"
