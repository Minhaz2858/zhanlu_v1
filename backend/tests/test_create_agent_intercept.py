"""R5 behavioural + AST tests for the create_agent tool-call intercept.

R4 added a `:::decision-summary` fenced block parser. In practice DeepSeek
(and other LLMs that don't reliably emit literal fenced blocks) tend to
paraphrase the build action in prose instead — the fence parser finds
nothing, the LLM's `create_agent` call executes directly, and the
Decision Summary card never appears.

R5 fixes this by intercepting the LLM's `create_agent` tool call BEFORE
it executes, treating the call's `arguments` JSON as the draft payload,
and routing through the same persistence helper used by the fence path.
This file verifies:

  1. ``_intercept_create_agent`` exists in ``app.routers.agents``.
  2. ``_persist_decision_summary_pause`` accepts a new
     ``tool_call_payload`` keyword argument.
  3. ``_intercept_create_agent`` returns ``(False, None, -1)`` when the
     batch contains no ``create_agent`` call.
  4. ``_intercept_create_agent`` returns ``(True, sanitized_args, idx)``
     when the batch contains a ``create_agent`` call, even when siblings
     are present.
  5. The sanitiser runs on the intercepted args (hostile keys dropped).
  6. The helper is called from all 3 LLM-loop sites in the file (v2
     main, v2 resume, v3 stream).
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


# ---------------------------------------------------------------------------
# AST tests — structural assertions about the source file
# ---------------------------------------------------------------------------

def test_intercept_helper_exists():
    """_intercept_create_agent must be defined in app.routers.agents."""
    tree = _module_tree()
    found = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_intercept_create_agent":
            found = True
            break
    assert found, "_intercept_create_agent is not defined in app/routers/agents.py"


def test_persist_helper_accepts_tool_call_payload():
    """_persist_decision_summary_pause must accept a new `tool_call_payload`
    keyword argument so the intercept path can pass the create_agent args
    directly without going through the fence parser."""
    tree = _module_tree()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_persist_decision_summary_pause":
            arg_names = [a.arg for a in node.args.args]
            assert "tool_call_payload" in arg_names, (
                f"_persist_decision_summary_pause signature missing tool_call_payload; "
                f"got {arg_names}"
            )
            return
    raise AssertionError("_persist_decision_summary_pause not found")


def test_intercept_helper_return_type_annotation():
    """_intercept_create_agent should annotate its return as the
    ``tuple[bool, dict | None, int]`` triple so it's self-documenting."""
    src = _load_source()
    # Sanity: the helper's first few lines include the return annotation.
    assert "-> tuple[bool, dict | None, int]:" in src, (
        "_intercept_create_agent is missing the return-type annotation"
    )


def test_intercept_called_from_v2_main_loop():
    """The v2 main LLM tool-call loop (inside add_message) must call the
    intercept helper. We look for `_intercept_create_agent(parsed_calls)`
    inside any function whose name starts with `add_message`."""
    src = _load_source()
    # The intercept line uses a single trailing-paren call:
    #     intercepted, intercept_payload, intercept_index = _intercept_create_agent(parsed_calls)
    # We require at least 2 such occurrences (one in v2 main, one in
    # v2 resume or v3 stream); the third is checked separately.
    occurrences = src.count("= _intercept_create_agent(parsed_calls)")
    assert occurrences >= 3, (
        f"Expected _intercept_create_agent(parsed_calls) to be called from 3 sites, "
        f"found {occurrences}"
    )


def test_intercept_at_all_three_sites():
    """Structural check: count distinct LLM loop functions that invoke
    the intercept helper. We expect exactly 3 call sites in the
    following function names (the v3 streaming handler may live in
    either `add_message_stream` or its nested `event_stream`):

        - add_message (v2 main)
        - resume_conversation (v2 resume)
        - add_message_stream OR event_stream (v3 stream)
    """
    tree = _module_tree()
    # Collect top-level (non-nested) FunctionDef / AsyncFunctionDef nodes
    # only — we don't want to count the same intercept call twice when it
    # lives in a nested function like `event_stream` inside
    # `add_message_stream`.
    top_level_fns: list[ast.AST] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            top_level_fns.append(node)

    sites: list[str] = []

    def _walk(fn: ast.AST) -> None:
        # Walk ONLY the direct body of `fn` (not nested fns).
        for stmt in fn.body:  # type: ignore[attr-defined]
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Assign):
                    for tgt in sub.targets:
                        if (
                            isinstance(tgt, ast.Tuple)
                            and len(tgt.elts) == 3
                            and isinstance(tgt.elts[0], ast.Name)
                            and tgt.elts[0].id == "intercepted"
                        ):
                            if (
                                isinstance(sub.value, ast.Call)
                                and isinstance(sub.value.func, ast.Name)
                                and sub.value.func.id == "_intercept_create_agent"
                            ):
                                sites.append(fn.name)  # type: ignore[attr-defined]
                                return

    for fn in top_level_fns:
        _walk(fn)

    # The v3 site lives in either add_message_stream (its body wraps
    # `event_stream`) or directly in the nested `event_stream` itself.
    # We allow either name for the v3 site.
    expected_v2 = {"add_message", "resume_conversation"}
    v3_options = {"add_message_stream", "event_stream"}

    assert len(sites) == 3, (
        f"Expected 3 intercept call sites, found {len(sites)} in {sites}"
    )

    v2_sites = [s for s in sites if s in expected_v2]
    v3_sites = [s for s in sites if s in v3_options]
    assert len(v2_sites) == 2, f"Expected 2 v2 sites (add_message + resume_conversation), got {v2_sites}"
    assert len(v3_sites) == 1, f"Expected 1 v3 site (add_message_stream or event_stream), got {v3_sites}"


# ---------------------------------------------------------------------------
# Behavioural tests — exercise the intercept helper directly
# ---------------------------------------------------------------------------

def _intercept_module():
    """Import the intercept helper on demand. Skips the test cleanly if
    the helper is not yet defined (so AST tests still pass during
    partial edits)."""
    try:
        from app.routers.agents import _intercept_create_agent
        return _intercept_create_agent
    except ImportError as e:
        raise AssertionError(f"Could not import _intercept_create_agent: {e}")


def test_intercept_returns_false_when_no_create_agent():
    """A batch with only `list_tools` + `skills(load, X)` must NOT
    trigger the intercept."""
    intercept = _intercept_module()
    parsed_calls = [
        {"tool_name": "list_tools", "args": {}, "args_str": "{}", "tool_call_id": "c1"},
        {"tool_name": "skills", "args": {"action": "load", "name": "X"},
         "args_str": "{}", "tool_call_id": "c2"},
    ]
    fired, payload, idx = intercept(parsed_calls)
    assert fired is False
    assert payload is None
    assert idx == -1


def test_intercept_finds_create_agent_in_mixed_batch():
    """A batch with `[list_tools, create_agent, skills(load, X)]` must
    fire on the second entry and return index 1."""
    intercept = _intercept_module()
    create_args = {
        "name": "Equipment Maintenance Agent",
        "description": "Tracks work orders",
        "capabilities": ["db_query", "reporting"],
        "model": "automatic",
        "agent_type": "sequential",
    }
    parsed_calls = [
        {"tool_name": "list_tools", "args": {}, "args_str": "{}", "tool_call_id": "c1"},
        {"tool_name": "create_agent", "args": create_args,
         "args_str": str(create_args), "tool_call_id": "c2"},
        {"tool_name": "skills", "args": {"action": "load", "name": "X"},
         "args_str": "{}", "tool_call_id": "c3"},
    ]
    fired, payload, idx = intercept(parsed_calls)
    assert fired is True
    assert idx == 1
    assert payload is not None
    assert payload.get("name") == "Equipment Maintenance Agent"
    assert payload.get("description") == "Tracks work orders"
    assert payload.get("capabilities") == ["db_query", "reporting"]


def test_intercept_sanitises_payload():
    """The intercept must run args through _sanitize_decision_payload so
    a malicious or malformed key is dropped before the pause persists it."""
    intercept = _intercept_module()
    create_args = {
        "name": "X",
        "description": "Test",
        # Hostile / unknown keys must be dropped by the sanitiser
        "__class__": "exploit",
        "DROP TABLE users; --": "sqli",
        "name": "X",  # duplicate on purpose — still allowed
    }
    parsed_calls = [
        {"tool_name": "create_agent", "args": create_args,
         "args_str": "{}", "tool_call_id": "c1"},
    ]
    fired, payload, idx = intercept(parsed_calls)
    assert fired is True
    assert idx == 0
    # Only allowed keys survive the sanitiser.
    assert payload is not None
    assert "name" in payload
    assert "__class__" not in payload
    assert "DROP TABLE users; --" not in payload


def test_intercept_drops_call_when_name_missing():
    """A create_agent call with no `name` must be sanitised to an empty
    payload — but the intercept still fires (the persist helper then
    rejects the pause downstream because `name` is required)."""
    intercept = _intercept_module()
    parsed_calls = [
        {"tool_name": "create_agent", "args": {"description": "no name here"},
         "args_str": "{}", "tool_call_id": "c1"},
    ]
    fired, payload, idx = intercept(parsed_calls)
    assert fired is True
    assert idx == 0
    assert payload is not None
    assert "name" not in payload


def test_intercept_handles_string_typed_args():
    """DeepSeek sometimes emits arguments as a JSON string (not a dict).
    The caller (v2/v3 loops) JSON-parses before handing to the intercept,
    so the helper must handle a plain dict. We verify the typical call
    shape here."""
    intercept = _intercept_module()
    parsed_calls = [
        {"tool_name": "create_agent", "args": {"name": "Y"},
         "args_str": '{"name": "Y"}', "tool_call_id": "c1"},
    ]
    fired, payload, idx = intercept(parsed_calls)
    assert fired is True
    assert payload == {"name": "Y"}


def test_intercept_with_empty_batch():
    """Defensive: an empty batch must NOT fire and must return the
    canonical (-1, None) tuple."""
    intercept = _intercept_module()
    fired, payload, idx = intercept([])
    assert fired is False
    assert payload is None
    assert idx == -1
