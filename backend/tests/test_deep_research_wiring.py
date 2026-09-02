"""Structural (AST) tests: deep-research mode must be wired into the v3
stream route, flag-gated, and backed by real LLM/tool implementations.

Red-first: fails until ``agents.py`` defines ``_stream_deep_research_frames``
+ the ``event_stream`` research branch and
``app/services/deep_research/llm_backend.py`` exists.
"""
import ast
import os

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_AGENTS_PATH = os.path.join(_BACKEND_ROOT, "app", "routers", "agents.py")
_LLM_BACKEND_PATH = os.path.join(
    _BACKEND_ROOT, "app", "services", "deep_research", "llm_backend.py"
)


def _load(path: str) -> str:
    with open(path) as f:
        return f.read()


def _defines(source: str, name: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return True
    return False


def _references(source: str, name: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id == name:
            return True
        if isinstance(node, ast.Attribute) and node.attr == name:
            return True
    return False


def _calls(source: str, name: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == name:
                return True
            if isinstance(fn, ast.Attribute) and fn.attr == name:
                return True
    return False


def test_agents_defines_research_stream_helper():
    source = _load(_AGENTS_PATH)
    assert _defines(source, "_stream_deep_research_frames"), (
        "agents.py must define _stream_deep_research_frames"
    )


def _has_string_constant(source: str, value: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == value:
            return True
    return False


def test_agents_routes_deep_research_flag_gated():
    source = _load(_AGENTS_PATH)
    # The repo convention gates flags via getattr(settings, "FLAG", False).
    assert _has_string_constant(source, "DEEP_RESEARCH_ENABLED"), (
        "agents.py must gate the research branch on DEEP_RESEARCH_ENABLED"
    )
    assert _references(source, "run_deep_research") or _calls(source, "run_deep_research"), (
        "agents.py must call run_deep_research"
    )


def test_llm_backend_builders_exist():
    source = _load(_LLM_BACKEND_PATH)
    for builder in ("build_planner", "build_tool_executor", "build_synthesizer", "build_critic"):
        assert _defines(source, builder), f"llm_backend.py must define {builder}"
