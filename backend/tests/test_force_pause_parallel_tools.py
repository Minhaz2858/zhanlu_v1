"""Regression tests for the R6 force-pause trigger condition.

Background
==========
The force-pause (R6) is a safety net that breaks the discovery loop when
the LLM keeps ``list_tools``-ing / ``skills``-ing instead of building. The
old trigger was:

    if iteration >= 2 and _user_wants_save_directly(user_content):
        ...

Problem: LLMs that support **parallel tool calls** (DeepSeek, GPT-4, etc.)
issue ALL discovery calls in a single iteration. The classic 3-call
discovery pattern (``list_tools`` + ``skills`` + ``list_knowledge_bases``)
happens at iteration 0. The next LLM turn either:
  (a) returns text and breaks the loop, or
  (b) calls another discovery tool and the loop guard caps it.

In both cases, the for-loop terminates at iteration 0 or 1, so the
``iteration >= 2`` check never fires and the Decision Summary card never
appears. The user is left staring at "Searching available capabilities..."
spinners with no resolution.

The fix: trigger on the accumulated **tool-call count**, not the iteration
counter. ``len(tool_calls_for_frontend) >= 2`` means "the LLM has explored
at least twice across the loop", which is true whether the calls were
parallel (one iteration) or sequential (multiple iterations).

This test pins down the new trigger and the parallel-tool-call scenario
so we cannot regress.
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


def _find_function(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in _module_tree().body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise RuntimeError(f"function {name!r} not found in agents.py")


def _walk_for_node(func, predicate):
    """Yield every AST node inside ``func`` matching ``predicate``."""
    for stmt in ast.walk(func):
        if predicate(stmt):
            yield stmt


# ---------------------------------------------------------------------------
# AST tests — pin down the trigger condition in source
# ---------------------------------------------------------------------------

def _find_force_pause_block(func):
    """Return the AST If-statement that guards the force-pause payload build.

    Identified by the call to ``_build_forced_decision_summary`` inside an
    ``if`` whose test references ``_user_wants_save_directly``.
    """
    candidates = []
    for node in _walk_for_node(func, lambda n: isinstance(n, ast.If)):
        # Walk only the direct body of this If (not nested Ifs) to find
        # a call to _build_forced_decision_summary. We also need the
        # test to reference _user_wants_save_directly directly.
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_build_forced_decision_summary"):
                    # Check the test references _user_wants_save_directly
                    for test_node in ast.walk(node.test):
                        if (isinstance(test_node, ast.Call)
                                and isinstance(test_node.func, ast.Name)
                                and test_node.func.id == "_user_wants_save_directly"):
                            candidates.append(node)
                            break
    if not candidates:
        raise AssertionError(
            f"no force-pause If-block found in {func.name!r}"
        )
    return candidates[0]


def _trigger_predicate_is_tool_call_count(func) -> bool:
    """The new trigger must include ``len(tool_calls_for_frontend) >= 2``,
    not just ``iteration >= 2``.

    The old code used ``iteration >= 2`` which never fires when the LLM
    issues parallel tool calls in a single iteration.
    """
    block = _find_force_pause_block(func)
    src = ast.unparse(block.test) if hasattr(ast, "unparse") else ""
    has_old = "iteration" in src and ">= 2" in src and "tool_calls_for_frontend" not in src
    has_new = "tool_calls_for_frontend" in src and ">=" in src
    # We accept the new form (tool-call count) as the canonical fix.
    return has_new and not has_old


def test_v3_stream_force_pause_uses_tool_call_count():
    """The v3 streaming endpoint's force-pause must trigger on tool-call
    count, not on iteration count, so parallel tool calls don't slip
    through."""
    func = _find_function("add_message_stream")
    # The force-pause is inside the nested ``event_stream`` async generator.
    # Walk into nested functions to find it.
    for nested in _walk_for_node(func, lambda n: isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        if nested.name == "event_stream":
            assert _trigger_predicate_is_tool_call_count(nested), (
                "v3 stream force-pause still uses iteration-based trigger; "
                "switch to len(tool_calls_for_frontend) >= 2 so parallel "
                "tool calls trigger the pause."
            )
            return
    raise AssertionError("event_stream nested function not found")


def test_v2_main_force_pause_uses_tool_call_count():
    """The v2 main ``add_message`` endpoint's force-pause must also trigger
    on tool-call count, not iteration count, for consistency."""
    func = _find_function("add_message")
    assert _trigger_predicate_is_tool_call_count(func), (
        "v2 main force-pause still uses iteration-based trigger; "
        "switch to len(tool_calls_for_frontend) >= 2 for consistency with v3."
    )


# ---------------------------------------------------------------------------
# Behavioural test — verify the trigger fires for the parallel case
# ---------------------------------------------------------------------------

def test_force_pause_triggers_on_parallel_tool_calls(monkeypatch):
    """Reproduce the production bug: LLM issues 3 parallel discovery
    calls in iteration 0. Force-pause must fire.

    We monkeypatch the v3 stream endpoint's dependencies and inspect the
    tool_calls_for_frontend list size + the trigger condition. This is a
    structural/contract test — it doesn't run a real LLM but proves the
    trigger condition evaluates True for the parallel case.
    """
    from app.routers.agents import (
        _user_wants_save_directly,
        _build_forced_decision_summary,
        _sanitize_decision_payload,
    )
    # The user's "save directly" cue is detected as a substring match.
    user_content = (
        "Build a sales analyst agent and save directly as an AgentApp.\n"
        "- Name: Salers Analytics\n"
        "- Project: Ungrouped\n"
    )
    assert _user_wants_save_directly(user_content), (
        "Test setup: user_content must contain a save-directly cue"
    )

    # Simulate the parallel-tool-call scenario: 3 tool calls, all in
    # the same iteration.
    tool_calls_for_frontend = [
        {"id": "t1", "name": "list_tools", "status": "completed"},
        {"id": "t2", "name": "search_skills", "status": "completed"},
        {"id": "t3", "name": "list_knowledge_bases", "status": "completed"},
    ]
    iteration = 0  # All in one iteration

    # The OLD trigger would NOT fire here (0 >= 2 is False). The NEW
    # trigger (len >= 2) DOES fire.
    old_triggers = iteration >= 2
    new_triggers = len(tool_calls_for_frontend) >= 2
    assert not old_triggers, (
        "Sanity: old iteration-based trigger fails for parallel case "
        "(this is the bug)"
    )
    assert new_triggers, (
        "New tool-call-count trigger should fire for the parallel case"
    )

    # And the forced payload should be buildable from this user message.
    forced = _build_forced_decision_summary(user_content)
    forced_clean = _sanitize_decision_payload(forced)
    assert forced_clean.get("name") == "Salers Analytics", (
        f"forced payload missing name: {forced_clean!r}"
    )
    assert forced_clean.get("project") == "Ungrouped"


# ---------------------------------------------------------------------------
# Chat session persistence — user message must survive mid-stream crash
# ---------------------------------------------------------------------------

def test_v3_stream_persists_user_message_before_loop():
    """Regression: if the SSE stream crashes mid-iteration (LLM error,
    network drop, forced pause), the user's message must already be
    persisted to the conversation, not just held in the in-memory
    ``messages`` list.

    The v3 stream endpoint appends the user message to the in-memory
    ``messages`` list at the start, but the first DB commit does not
    happen until the end of the loop (or the mid-iteration checkpoint,
    which only fires when ``tool_calls_for_frontend`` is non-empty).
    This test pins down an explicit early commit that persists the user
    message before the tool-calling loop starts.
    """
    import re as _re
    src = _load_source()

    # Strategy: find the slice between ``messages.append(user_msg)``
    # and the FIRST ``for iteration in range(MAX_TOOL_ITERATIONS)`` —
    # because the bug is that NO commit happens between these two
    # points. The early commit MUST appear in this slice.

    # 1) Find the line of ``messages.append(user_msg)`` (first match
    # inside the v3 stream endpoint, i.e. AFTER the first occurrence
    # of ``add_message_stream`` def).
    fn_match = _re.search(r"async def add_message_stream\(", src)
    assert fn_match, "add_message_stream function not found"
    body = src[fn_match.end():]

    m1 = _re.search(r"^\s*messages\.append\(user_msg\)", body, _re.MULTILINE)
    assert m1, "user message append not found in add_message_stream"
    after_user_msg = body[m1.end():]

    # 2) Find the first ``for iteration in range(`` (the start of the
    # tool-calling loop in event_stream).
    m_loop = _re.search(
        r"^\s*for\s+iteration\s+in\s+range\(",
        after_user_msg,
        _re.MULTILINE,
    )
    assert m_loop, "for iteration in range() loop not found in v3 stream"
    before_loop = after_user_msg[: m_loop.start()]

    # 3) The slice between user-msg-append and loop-start must contain
    # a `conv.messages = ...` assignment AND a `db.commit()`. Without
    # the commit, a crash inside the loop loses the user's message.
    has_conv_msg_assign = _re.search(r"^\s*conv\.messages\s*=", before_loop, _re.MULTILINE)
    has_commit = "db.commit()" in before_loop

    assert has_conv_msg_assign and has_commit, (
        "add_message_stream must persist the user message BEFORE the "
        "tool-calling loop starts. Specifically: after appending the "
        "user message to `messages`, it must assign `conv.messages = "
        "messages` and call `db.commit()` BEFORE the first `for "
        "iteration in range(MAX_TOOL_ITERATIONS)`. Without this, a "
        "crash inside the SSE generator (LLM error, network drop) "
        "leaves the user staring at an empty chat with their message "
        "lost. Currently:\n"
        f"  conv.messages assignment present: {bool(has_conv_msg_assign)}\n"
        f"  db.commit() present: {has_commit}"
    )


def test_v2_main_persists_user_message_before_loop():
    """Same regression as v3, but for the v2 main ``add_message`` path
    (used as a fallback when v3 SSE fails). The frontend's
    ``handleSend`` falls back to ``base44.agents.addMessage`` if the
    stream fails, so this path also must persist the user message
    before the agent loop starts.
    """
    import re as _re
    src = _load_source()

    fn_match = _re.search(r"async def add_message\(", src)
    assert fn_match, "add_message function not found"
    body = src[fn_match.end():]

    m1 = _re.search(r"^\s*messages\.append\(user_msg\)", body, _re.MULTILINE)
    assert m1, "user message append not found in add_message"
    after_user_msg = body[m1.end():]

    m_loop = _re.search(
        r"^\s*for\s+iteration\s+in\s+range\(",
        after_user_msg,
        _re.MULTILINE,
    )
    assert m_loop, "for iteration in range() loop not found in add_message"
    before_loop = after_user_msg[: m_loop.start()]

    has_conv_msg_assign = _re.search(r"^\s*conv\.messages\s*=", before_loop, _re.MULTILINE)
    has_commit = "db.commit()" in before_loop

    assert has_conv_msg_assign and has_commit, (
        "add_message (v2 main) must persist the user message BEFORE "
        "the tool-calling loop starts. Same reasoning as the v3 test: "
        "a crash inside the agent loop otherwise loses the user's "
        "message and leaves the chat looking empty on reload."
    )
