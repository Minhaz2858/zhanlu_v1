"""P0 pilot fix (2026-10-31): EVERY registered tool schema must be a valid
OpenAI envelope. The six universal_* tools registered bare parameter schemas
(no name/envelope) — strict providers (DashScope qwen) 400 the whole request
for any agent whose toolset included them (live failure: tools.6/tools.7
"'function' is a required property"). DeepSeek tolerated the shape, so this
only surfaced on the qwen path. This test locks the invariant for ALL tools,
present and future."""
import pkgutil
import importlib

import app.services.tool_handlers as _pkg  # noqa: F401


def _all_registered():
    for m in pkgutil.iter_modules(_pkg.__path__):
        importlib.import_module("app.services.tool_handlers." + m.name)
    from app.services.tool_registry import registry

    return registry


def test_every_registered_tool_schema_is_envelope_valid():
    registry = _all_registered()
    bad = []
    for name in registry.list_names():
        entry = registry.get_entry(name)
        s = entry.schema if entry else None
        if not isinstance(s, dict):
            bad.append((name, "schema is not a dict"))
            continue
        fn = s.get("function")
        if s.get("type") != "function" or not isinstance(fn, dict):
            bad.append((name, "missing {type: function, function} envelope"))
            continue
        if not fn.get("name"):
            bad.append((name, "envelope missing function.name"))
        params = fn.get("parameters")
        if not isinstance(params, dict) or params.get("type") != "object":
            bad.append((name, "parameters must be a {type: object} schema"))
        elif "properties" not in params:
            bad.append((name, "parameters object schema missing properties"))
    assert not bad, (
        "registered tool schemas must be provider-valid envelopes; "
        f"offenders: {bad[:10]}"
    )


def test_universal_tools_have_descriptions():
    registry = _all_registered()
    for name in ("universal_query", "universal_kpi", "universal_trend",
                 "universal_forecast", "universal_describe", "universal_discover"):
        entry = registry.get_entry(name)
        assert entry is not None, f"{name} must be registered"
        desc = entry.schema["function"]["description"]
        assert desc, f"{name} envelope must carry a description"
