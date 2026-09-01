"""R6 behavioural + AST tests for the force-pause mechanism.

The force-pause kicks in when the LLM has been exploring for 2+
iterations without ever calling ``create_agent`` and the user message
contains a "save directly" / "build it" cue. The point is to break the
discovery loop deterministically when DeepSeek keeps
``list_tools``-ing / ``skills``-ing instead of building the agent.

This file exercises:
  1. ``_user_wants_save_directly`` correctly detects the cue phrases.
  2. ``_build_forced_decision_summary`` extracts Name/Project/Description
     from a typical save-directly prompt.
  3. The two helpers are referenced from the v2 main and v3 stream
     paths (AST assertion).
  4. Edge cases: no `name` extracted -> forced payload is empty.
  5. Full-width colons (Chinese/English "Name: X") are accepted.
"""
import ast
import os
import sys

_BACKEND_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _BACKEND_ROOT not in sys.path:
    sys.path.insert(0, _BACKEND_ROOT)
os.chdir(_BACKEND_ROOT)


_AGENTS_PATH = os.path.join(_BACKEND_ROOT, "app", "routers", "agents.py")


def _load_source() -> str:
    with open(_AGENTS_PATH, "r", encoding="utf-8") as f:
        return f.read()


def _module_tree() -> ast.Module:
    return ast.parse(_load_source())


def _helpers():
    from app.routers.agents import (
        _user_wants_save_directly,
        _build_forced_decision_summary,
    )
    return _user_wants_save_directly, _build_forced_decision_summary


# ---------------------------------------------------------------------------
# AST tests
# ---------------------------------------------------------------------------

def test_force_pause_helpers_defined():
    src = _load_source()
    assert "def _user_wants_save_directly" in src
    assert "def _build_forced_decision_summary" in src
    assert "_FORCE_PAUSE_INTENT_PHRASES" in src
    assert "_FORCE_PAUSE_SPEC_PATTERNS" in src


def test_force_pause_used_in_v2_main_and_v3_stream():
    """The two helpers must be called from at least the v2 main loop
    and the v3 stream event_stream (top-level functions)."""
    tree = _module_tree()
    top_level_fns: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top_level_fns.append(node)

    user_calls: list[str] = []
    build_calls: list[str] = []

    def _walk(fn: ast.AST) -> None:
        for stmt in fn.body:  # type: ignore[attr-defined]
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                    if sub.func.id == "_user_wants_save_directly":
                        user_calls.append(fn.name)  # type: ignore[attr-defined]
                    elif sub.func.id == "_build_forced_decision_summary":
                        build_calls.append(fn.name)  # type: ignore[attr-defined]

    for fn in top_level_fns:
        _walk(fn)

    assert len(user_calls) >= 2, (
        f"_user_wants_save_directly must be called from v2 main + v3 stream, "
        f"got {user_calls}"
    )
    assert len(build_calls) >= 2, (
        f"_build_forced_decision_summary must be called from v2 main + v3 stream, "
        f"got {build_calls}"
    )


# ---------------------------------------------------------------------------
# Behavioural tests
# ---------------------------------------------------------------------------

def test_user_wants_save_directly_english():
    user_wants, _ = _helpers()
    assert user_wants(
        "Please build me an agent based on the following info (save directly as an AgentApp):"
    )


def test_user_wants_save_directly_phrase_variants():
    user_wants, _ = _helpers()
    for phrase in (
        "save directly",
        "save it directly",
        "build it now",
        "build it directly",
        "create it now",
        "create it directly",
        "build directly",
        "create directly",
        "save as an agentapp",
        "save as agentapp",
    ):
        assert user_wants(f"hello {phrase} please"), f"failed to detect phrase: {phrase!r}"


def test_user_does_not_want_save_directly_by_default():
    user_wants, _ = _helpers()
    assert not user_wants("build me a sales agent")
    assert not user_wants("I want an agent for support")
    assert not user_wants("")
    assert not user_wants("   ")


def test_user_wants_save_directly_case_insensitive():
    user_wants, _ = _helpers()
    assert user_wants("Please SAVE DIRECTLY as an AgentApp")
    assert user_wants("Build It Now please")


def test_build_forced_decision_summary_extracts_name():
    _, build = _helpers()
    user_msg = (
        "Please build me an agent based on the following info (save directly as an AgentApp):\n"
        "- Name: Salers Analytics\n"
        "- Project: Ungrouped\n"
        "- Description: Analyzes sales data and generates reports.\n"
    )
    payload = build(user_msg)
    assert payload.get("name") == "Salers Analytics"
    assert payload.get("project") == "Ungrouped"
    assert "sales data" in payload.get("description", "")


def test_build_forced_decision_summary_full_width_colon():
    """Chinese input often uses the full-width colon : instead of :.
    Both must work."""
    _, build = _helpers()
    user_msg = (
        "Please build an agent (save directly as an AgentApp):\n"
        "- Name: Salers Analysis\n"
        "- Project: Ungrouped\n"
        "- Description: Analyzes sales data and generates reports.\n"
    )
    payload = build(user_msg)
    assert payload.get("name") == "Salers Analysis"
    assert payload.get("project") == "Ungrouped"
    assert "sales data" in payload.get("description", "")


def test_build_forced_decision_summary_applies_defaults():
    """Sensible defaults for everything the user did not specify."""
    _, build = _helpers()
    user_msg = (
        "save directly:\n"
        "- Name: Minimal\n"
    )
    payload = build(user_msg)
    assert payload.get("name") == "Minimal"
    assert payload.get("model") == "automatic"
    assert payload.get("agent_type") == "sequential"
    assert payload.get("data_read") is True
    assert payload.get("data_write") is False
    assert payload.get("human_fallback") is True
    assert payload.get("trace_enabled") is True
    assert payload.get("capabilities") == []
    assert payload.get("skills") == []


def test_build_forced_decision_summary_empty_when_no_name():
    """If we can't extract a name, the forced payload has no 'name' key
    so the caller treats it as 'can't force-pause' and falls through."""
    _, build = _helpers()
    payload = build("save directly please, but I forgot to give you a name")
    assert "name" not in payload


def test_build_forced_decision_summary_handles_missing_optional_fields():
    """Project and Description are optional; only Name is required."""
    _, build = _helpers()
    user_msg = "save directly please:\n- Name: OnlyName\n"
    payload = build(user_msg)
    assert payload.get("name") == "OnlyName"
    assert "project" not in payload or payload.get("project") == ""
    assert "description" not in payload or payload.get("description") == ""


def test_build_forced_decision_summary_alternate_bullets():
    """Accept * bullet, hyphen, or no bullet at all (for free-form input)."""
    _, build = _helpers()
    for variant in (
        "- Name: A",
        "* Name: A",
        "Name: A",
    ):
        payload = build(f"save directly:\n{variant}")
        assert payload.get("name") == "A", f"failed to extract name from: {variant!r}"


def test_build_forced_decision_summary_no_crash_on_empty():
    _, build = _helpers()
    # Should never raise
    assert isinstance(build(""), dict)
    assert isinstance(build(None), dict)
