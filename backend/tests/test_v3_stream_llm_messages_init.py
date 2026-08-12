"""Regression test for the v3 stream ``UnboundLocalError`` on ``llm_messages``.

Root cause: in ``add_message_stream`` (v3 SSE endpoint), ``llm_messages``
was once assigned only inside an optional routing block:

    if not llm_messages:                    # <-- UnboundLocalError when fast path inactive
        llm_messages = [{"role": "system", "content": system_prompt}]

For the common case the optional branch never ran, so ``llm_messages`` was
never assigned when the post-block guard tried to read it. The resulting UnboundLocalError killed
the SSE generator mid-stream — the frontend showed
"Sorry, the connection was interrupted. Please try again." with an empty
assistant bubble (every regular chat request broke).

Fix: initialize ``llm_messages`` directly with the system prompt before
history reconstruction.
"""
import ast
import os

_AGENTS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "agents.py"
)


def _find_v3_function(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "add_message_stream":
            return node
    raise RuntimeError("add_message_stream not found in agents.py")


def _first_assignment_line(func, name, value_predicate):
    """Return the lineno of the first ``name = ...`` assignment whose RHS
    satisfies ``value_predicate(value_node)``. None if no such assignment.
    """
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not (isinstance(target, ast.Name) and target.id == name):
                continue
            if value_predicate(node.value):
                return node.lineno
    return None


def test_llm_messages_initialized_with_system_prompt():
    """v3 must initialize ``llm_messages`` with the system prompt."""
    with open(_AGENTS_PATH) as f:
        source = f.read()
    tree = ast.parse(source)
    func = _find_v3_function(tree)

    init_line = _first_assignment_line(
        func,
        "llm_messages",
        lambda v: (
            isinstance(v, ast.List)
            and len(v.elts) == 1
            and isinstance(v.elts[0], ast.Dict)
        ),
    )
    assert init_line is not None, (
        "No ``llm_messages = [{...system_prompt...}]`` initialization found "
        "in add_message_stream. Initialize llm_messages before history "
        "reconstruction so the SSE stream always has a system message."
    )


def test_llm_messages_history_reconstruction_uses_initialized_list():
    """History reconstruction should append to the initialized list."""
    with open(_AGENTS_PATH) as f:
        source = f.read()
    tree = ast.parse(source)
    func = _find_v3_function(tree)
    append_lines = []
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "append" and isinstance(node.func.value, ast.Name) and node.func.value.id == "llm_messages":
                append_lines.append(node.lineno)
    assert append_lines, "Expected history reconstruction to append to llm_messages"