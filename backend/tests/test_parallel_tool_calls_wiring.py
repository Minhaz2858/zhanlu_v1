"""Structural (AST) tests: the main-agent-loop payload builders must route
the parallel-tool-calls capability through the shared helper
``llm_service.maybe_enable_parallel_tool_calls`` so the
``LLM_PARALLEL_TOOL_CALLS_ENABLED`` flag actually applies everywhere the
agent loop builds an LLM request payload.

Red-first: these fail until ``_call_llm_with_tools`` and
``_stream_llm_with_tools`` in ``agents.py`` wire the helper in.
"""
import ast
import os

_AGENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "agents.py"
)


def _load_source() -> str:
    with open(_AGENTS_PATH) as f:
        return f.read()


def _find_function(source: str, name: str):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _contains_helper_call(func) -> bool:
    """True if the function body calls ``maybe_enable_parallel_tool_calls``
    (as a bare name or a module attribute)."""
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == "maybe_enable_parallel_tool_calls":
                return True
            if (
                isinstance(fn, ast.Attribute)
                and fn.attr == "maybe_enable_parallel_tool_calls"
            ):
                return True
    return False


def test_call_llm_with_tools_wires_parallel_capability():
    func = _find_function(_load_source(), "_call_llm_with_tools")
    assert func is not None
    assert _contains_helper_call(func), (
        "_call_llm_with_tools must call "
        "maybe_enable_parallel_tool_calls(payload, model)"
    )


def test_stream_llm_with_tools_wires_parallel_capability():
    func = _find_function(_load_source(), "_stream_llm_with_tools")
    assert func is not None
    assert _contains_helper_call(func), (
        "_stream_llm_with_tools must call "
        "maybe_enable_parallel_tool_calls(base_payload, model)"
    )
