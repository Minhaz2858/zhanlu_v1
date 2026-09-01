"""Regression test for the v3 streaming endpoint DSML leak.

Root cause: ``add_message_stream`` (POST /v3/.../messages/stream) re-calls
the LLM with ``tools=None`` and ``stream=True`` after the main tool-calling
loop has resolved all tool calls. When the conversation history still
contains ``assistant`` messages with ``tool_calls`` and the model "wants"
to make another tool call, the DeepSeek model cannot return structured
``tool_calls`` (no ``tools`` param), so it falls back to emitting its
native DSML tokens as plain text content:

    <｜｜DSML｜｜tool_calls>
    <｜｜DSML｜｜invoke name="create_agent">
    ...
    </｜｜DSML｜｜invoke>
    </｜｜DSML｜｜tool_calls>

This raw token soup then streams to the UI and gets rendered inside the
assistant chat bubble — the "raw text" symptom seen in the Agent Builder.

This test pins down the fix: the v3 streaming endpoint must NOT do a
second LLM call. It must use the ``assistant_content`` already obtained
from the main loop's non-streaming ``_call_llm_with_tools`` call — exactly
like the non-streaming v2 ``add_message`` does.

A pure end-to-end test of the SSE endpoint requires a real LLM + DB stack;
this test does the minimum reproducible check: it parses the source AST
and asserts the v3 endpoint does not call ``_stream_llm_final_response``
in its post-loop block.
"""
import ast
import os
import sys

_AGENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "agents.py"
)


def _load_source():
    with open(_AGENTS_PATH) as f:
        return f.read()


def _find_function(source, name):
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return source, node
    raise RuntimeError(f"function {name!r} not found in agents.py")


def _find_v3_stream_endpoint_block(func):
    """Return the AST node for the ``async for event_type, event_data in
    _stream_llm_final_response(...)`` call inside ``add_message_stream``,
    or None if not present.
    """
    for node in ast.walk(func):
        if not isinstance(node, ast.AsyncFor):
            continue
        # The async-for's iter is a Call to _stream_llm_final_response
        if not isinstance(node.iter, ast.Call):
            continue
        if isinstance(node.iter.func, ast.Name) and node.iter.func.id == "_stream_llm_final_response":
            return node
        if isinstance(node.iter.func, ast.Attribute) and node.iter.func.attr == "_stream_llm_final_response":
            return node
    return None


def test_v3_streaming_endpoint_does_not_recall_llm_with_tools_none():
    """The post-loop "re-stream" block in ``add_message_stream`` is the
    source of the DSML leak. Pin down that it's been removed.

    Two acceptable outcomes:
      1. The dangerous ``async for ... in _stream_llm_final_response(...)``
         call is gone entirely (preferred — matches v2 non-streaming path).
      2. It still exists but does NOT pass ``tools=None`` (we'd need a
         different fix in that case; flag it for human review).
    """
    _, func = _find_function(_load_source(), "add_message_stream")
    recall = _find_v3_stream_endpoint_block(func)

    if recall is None:
        # Preferred fix: dangerous re-call has been removed.
        return

    # If the re-call still exists, ensure it does NOT pass tools=None.
    call = recall.iter  # the Call to _stream_llm_final_response
    tools_kwarg = None
    for kw in call.keywords:
        if kw.arg == "tools":
            tools_kwarg = kw.value
            break
    assert tools_kwarg is not None, (
        "If _stream_llm_final_response is called in add_message_stream, "
        "the `tools` kwarg must be passed explicitly (not relying on the "
        "default). Otherwise the DeepSeek model can fall back to emitting "
        "DSML tokens as plain text content when it wants to call a tool."
    )

    # If `tools=None` is passed, that's still the bug — fail.
    if isinstance(tools_kwarg, ast.Constant) and tools_kwarg.value is None:
        pytest.fail(
            "add_message_stream still calls _stream_llm_final_response(tools=None). "
            "This is the source of the DSML leak: when the model wants to call a "
            "tool but no `tools` param is provided, DeepSeek emits its native "
            "DSML tokens as plain text content. Drop the re-call and use the "
            "assistant_content already captured by the main loop's "
            "_call_llm_with_tools call."
        )


def test_v3_streaming_uses_assistant_content_directly_for_final_message():
    """After the main tool-calling loop, the v3 endpoint must persist and
    stream the ``assistant_content`` it already has — not a re-generated
    version. This is the structural match to the v2 non-streaming path
    (which uses ``assistant_content = llm_response.get('content', '')``
    directly with no second call).
    """
    _, func = _find_function(_load_source(), "add_message_stream")
    src = ast.unparse(func) if hasattr(ast, "unparse") else ""
    # The function should set assistant_content from the main loop's
    # llm_response. This was already the case (line 1851), so this is
    # mostly a sanity check that the v3 endpoint uses the same field
    # when building the final assistant message.
    assert "assistant_content = llm_response.get" in src or "assistant_content=llm_response" in src, (
        "add_message_stream must read assistant_content from the main loop's "
        "non-streaming LLM response (the same field used by add_message v2)."
    )


def test_v3_reasoning_does_not_pollute_assistant_content():
    """P0 reasoning extraction: the reasoning captured from the LLM message
    must be persisted as a separate key on assistant_msg — never appended
    into assistant_content. This protects the model context window from
    pollution on compaction.
    """
    _, func = _find_function(_load_source(), "add_message_stream")
    src = ast.unparse(func) if hasattr(ast, "unparse") else ""
    # Reasoning must be present as a separate key
    assert '"reasoning"' in src or "'reasoning'" in src, (
        "v3 must persist reasoning as a separate field on the assistant message"
    )
    # No augmented assignment that combines reasoning into assistant_content
    # (e.g. `assistant_content += reasoning` — that would be context pollution)
    for node in ast.walk(func):
        if isinstance(node, ast.AugAssign):
            target = ast.unparse(node.target)
            value = ast.unparse(node.value)
            if target == "assistant_content" and "reasoning" in value:
                raise AssertionError(
                    "v3 must NOT append reasoning into assistant_content "
                    f"(found `{target} {ast.unparse(node.op)} {value}`)"
                )


def test_v3_reasoning_assignment_is_in_scope():
    """Regression guard for the B1 blocker caught by code review: any
    `assistant_msg[...] = ...` assignment must come AFTER the dict
    `assistant_msg = {...}` is built. We assert that the first
    `assistant_msg[...]=` (non-AugAssign) statement that touches
    'reasoning' is preceded by a plain `assistant_msg = {...}` literal.

    The pattern this catches:
        llm_response = await _call_llm_with_tools(...)
        # ... inside the tool loop ...
        assistant_msg["reasoning"] = ...   # NameError: assistant_msg undefined
    """
    _, func = _find_function(_load_source(), "add_message_stream")
    # Build a list of (line, action) for assistant_msg-related statements
    actions = []
    for node in ast.walk(func):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                target_src = ast.unparse(target)
                if target_src == "assistant_msg":
                    actions.append(("define", node.lineno, ast.unparse(node.value)[:60]))
                elif target_src.startswith("assistant_msg["):
                    key = target_src
                    actions.append(("assign", node.lineno, key, ast.unparse(node.value)[:60]))
    # Find the first define of assistant_msg
    first_define = next((a for a in actions if a[0] == "define"), None)
    assert first_define is not None, (
        "add_message_stream must build an assistant_msg dict literal"
    )
    # All subsequent assistant_msg[...]= assignments must come AFTER first_define
    for action in actions:
        if action[0] == "assign" and action[1] < first_define[1]:
            raise AssertionError(
                f"assistant_msg[...]= assignment on line {action[1]} comes "
                f"BEFORE the assistant_msg dict literal on line {first_define[1]} — "
                f"NameError risk. Move the assignment after the dict literal."
            )


# Local import to avoid pulling pytest as a hard dep on the existing
# test runner (some tests use unittest only).
import pytest  # noqa: E402
