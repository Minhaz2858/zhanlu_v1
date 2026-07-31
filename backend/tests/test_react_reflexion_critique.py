"""Tests for the ReAct reflexion critique system message (Item 8).

The raw ReAct loop (duplicated across ``add_message``, ``resume_conversation``,
and ``add_message_stream``) used to feed tool failures back as plain
``role:"tool"`` result messages, leaving the LLM to retry blindly.  Item 8
adds a shared ``_inject_reflexion_critique`` helper that appends a
``role:"system"`` critique message after a batch of tool results whenever any
result has ``success is False`` with a truthy ``error``.

This file verifies:

  1. ``_inject_reflexion_critique`` is defined in ``app.routers.agents``.
  2. It is called from all 3 raw-loop sites (6 call points: main + sibling
     at each site).
  3. Behavioural correctness: no-op on success, fires on failure, handles
     multiple failures, ignores approval-paused / empty-data results, and
     truncates overly long argument strings.
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
# AST / structural tests
# ---------------------------------------------------------------------------

def test_helper_exists():
    """_inject_reflexion_critique must be defined in app.routers.agents."""
    tree = _module_tree()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_inject_reflexion_critique":
            return
    raise AssertionError("_inject_reflexion_critique is not defined in app/routers/agents.py")


def test_helper_called_from_all_three_sites():
    """The helper must be invoked from all 3 raw-loop functions:

        - add_message          (v2 main)
        - resume_conversation  (v2 resume)
        - add_message_stream   (v3 stream)

    Each site has a main loop and a sibling (intercept) loop, so we expect
    at least 6 call occurrences total.
    """
    src = _load_source()
    occurrences = src.count("_inject_reflexion_critique(")
    # 1 is the ``def`` line itself; the rest are call sites.
    call_sites = occurrences - 1
    assert call_sites >= 6, (
        f"Expected _inject_reflexion_critique to be called from >= 6 sites "
        f"(3 main + 3 sibling), found {call_sites}"
    )


def test_helper_called_from_expected_functions():
    """Walk top-level functions and confirm the 3 expected function names
    each contain at least one call to _inject_reflexion_critique."""
    tree = _module_tree()
    expected = {"add_message", "resume_conversation", "add_message_stream"}
    found: set[str] = set()

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in expected:
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id == "_inject_reflexion_critique"
            ):
                found.add(node.name)
                break

    missing = expected - found
    assert not missing, (
        f"_inject_reflexion_critique is not called from: {missing}"
    )


# ---------------------------------------------------------------------------
# Behavioural tests — exercise the helper directly
# ---------------------------------------------------------------------------

def _helper():
    """Import the helper on demand."""
    from app.routers.agents import _inject_reflexion_critique
    return _inject_reflexion_critique


def _call(name="search_skills", args_str='{"query": "foo"}'):
    return {"tool_name": name, "args": {}, "args_str": args_str, "tool_call_id": "tc1"}


def test_noop_when_all_succeed():
    """No system message should be appended when every result succeeds."""
    inject = _helper()
    msgs: list[dict] = [{"role": "user", "content": "hi"}]
    inject(msgs, [_call()], [{"success": True, "results": []}])
    assert len(msgs) == 1  # unchanged


def test_noop_when_success_falsy_but_no_error():
    """A result with success falsy but no 'error' key (e.g. empty-data)
    must NOT trigger the critique — only real failures with an error do."""
    inject = _helper()
    msgs: list[dict] = []
    inject(msgs, [_call()], [{"rows": []}])
    assert msgs == []


def test_noop_on_approval_pause():
    """requires_approval results are not failures and must be ignored."""
    inject = _helper()
    msgs: list[dict] = []
    inject(msgs, [_call()], [{"requires_approval": True, "approval_id": "ap1"}])
    assert msgs == []


def test_injects_system_message_on_failure():
    """A single failed tool must produce exactly one system message
    containing the tool name and the error text."""
    inject = _helper()
    msgs: list[dict] = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1"}]},
        {"role": "tool", "tool_call_id": "tc1", "content": '{"success": false}'},
    ]
    inject(msgs, [_call(name="ask_data_agent")], [{"success": False, "error": "column 'x' not found"}])
    assert len(msgs) == 3
    critique = msgs[-1]
    assert critique["role"] == "system"
    content = critique["content"]
    assert "ask_data_agent" in content
    assert "column 'x' not found" in content
    assert "reflect" in content.lower() or "identif" in content.lower()


def test_injects_after_tool_results_not_interleaved():
    """The critique system message must be the LAST message — it must
    come after all role:'tool' messages, never between them, so the
    OpenAI-compatible API doesn't reject the message ordering."""
    inject = _helper()
    calls = [_call(args_str='{"q": 1}'), _call(args_str='{"q": 2}')]
    results = [
        {"success": False, "error": "boom"},
        {"success": True, "data": "ok"},
    ]
    msgs: list[dict] = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "tc1"}, {"id": "tc2"}]},
        {"role": "tool", "tool_call_id": "tc1", "content": "{}"},
        {"role": "tool", "tool_call_id": "tc2", "content": "{}"},
    ]
    inject(msgs, calls, results)
    # The system message must be the very last message.
    assert msgs[-1]["role"] == "system"
    # The two tool messages must still be consecutive (not split by system).
    roles = [m["role"] for m in msgs]
    assert roles == ["assistant", "tool", "tool", "system"]


def test_multiple_failures_listed():
    """When multiple tools fail, the critique must mention each one."""
    inject = _helper()
    calls = [
        _call(name="search_skills", args_str='{"query": "a"}'),
        _call(name="ask_data_agent", args_str='{"question": "b"}'),
    ]
    results = [
        {"success": False, "error": "timeout"},
        {"success": False, "error": "no such table"},
    ]
    msgs: list[dict] = []
    inject(msgs, calls, results)
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert "search_skills" in content
    assert "ask_data_agent" in content
    assert "timeout" in content
    assert "no such table" in content


def test_long_args_truncated():
    """Argument strings longer than 200 chars must be truncated with an
    ellipsis so the critique message doesn't balloon on huge payloads."""
    inject = _helper()
    long_args = '{"prompt": "' + "x" * 500 + '"}'
    calls = [_call(name="create_artifact", args_str=long_args)]
    results = [{"success": False, "error": "too large"}]
    msgs: list[dict] = []
    inject(msgs, calls, results)
    content = msgs[0]["content"]
    assert "…" in content
    # The full 500-char string must NOT appear verbatim.
    assert "x" * 500 not in content


def test_non_dict_results_ignored():
    """If a result is not a dict (defensive — shouldn't happen but the
    helper must not crash), it must be silently skipped."""
    inject = _helper()
    msgs: list[dict] = []
    inject(msgs, [_call()], [None])  # type: ignore[list-item]
    assert msgs == []


def test_mixed_success_and_failure():
    """A batch with one success and one failure must still inject the
    critique, mentioning only the failed tool."""
    inject = _helper()
    calls = [_call(name="list_tools"), _call(name="search_skills")]
    results = [
        {"success": True, "tools": []},
        {"success": False, "error": "index unavailable"},
    ]
    msgs: list[dict] = []
    inject(msgs, calls, results)
    assert len(msgs) == 1
    content = msgs[0]["content"]
    assert "search_skills" in content
    assert "index unavailable" in content
    # The successful tool should NOT be listed as a failure.
    assert "list_tools" not in content


def test_success_false_without_error_ignored():
    """A result with success is False but no 'error' key must NOT trigger
    the critique — only failures that carry an explanatory error do."""
    inject = _helper()
    msgs: list[dict] = []
    inject(msgs, [_call()], [{"success": False}])
    assert msgs == []
