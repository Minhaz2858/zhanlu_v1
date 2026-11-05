"""P0 pilot fix: strict providers (DashScope qwen) 400 on {"type":"object"}
schemas without "properties". The sanitizer must patch them everywhere."""
from app.services.tool_schema_sanitize import sanitize_tool_schemas


def _wrapped(params):
    return {
        "type": "function",
        "function": {
            "name": "t",
            "description": "",
            "parameters": params,
        },
    }


def test_bare_object_schema_gets_empty_properties():
    tools = [_wrapped({"type": "object"})]
    out = sanitize_tool_schemas(tools)
    assert out[0]["function"]["parameters"]["properties"] == {}


def test_nested_bare_object_patched():
    tools = [_wrapped({
        "type": "object",
        "properties": {
            "chart": {"type": "object"},
            "filters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    })]
    out = sanitize_tool_schemas(tools)
    params = out[0]["function"]["parameters"]
    assert params["properties"]["chart"]["properties"] == {}
    assert params["properties"]["filters"]["properties"] == {"q": {"type": "string"}}


def test_originals_not_mutated():
    tools = [_wrapped({"type": "object"})]
    sanitize_tool_schemas(tools)
    assert "properties" not in tools[0]["function"]["parameters"], (
        "the caller's payload must not be mutated in place"
    )


def test_none_and_empty_passthrough():
    assert sanitize_tool_schemas(None) is None
    assert sanitize_tool_schemas([]) == []


def test_flat_form_tool_gets_function_envelope():
    flat = {"name": "universal_query", "description": "d",
            "type": "object", "properties": {"q": {"type": "string"}}}
    out = sanitize_tool_schemas([flat])
    fn = out[0]["function"]
    assert fn["name"] == "universal_query"
    assert fn["parameters"]["properties"] == {"q": {"type": "string"}}


def test_flat_form_bare_parameters_wrapped_and_patched():
    flat = {"name": "weird_tool", "parameters": {"type": "object"}}
    out = sanitize_tool_schemas([flat])
    fn = out[0]["function"]
    assert fn["name"] == "weird_tool"
    assert fn["parameters"]["properties"] == {}
