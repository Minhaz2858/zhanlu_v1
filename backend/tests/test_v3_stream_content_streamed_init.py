"""Regression test for the v3 stream ``UnboundLocalError``.

Root cause: in ``add_message_stream`` (v3 SSE endpoint),
``content_streamed`` was initialized INSIDE the tool-calling loop but
read AFTER it (final delta emit + paused branch). Two loop exits can
fire on the FIRST iteration, before the initialization line runs:

1. the conversation-iteration-budget break, and
2. the tool-call loop-guard break (which scans message history and can
   trip immediately when earlier turns already contain repeated
   identical calls, e.g. the automation "Run Now" flow).

When either fired first, the post-loop ``if not content_streamed:``
read raised ``UnboundLocalError``, killing the SSE generator mid-stream
— the frontend showed "Sorry, the connection was interrupted. Please
try again." with an empty assistant bubble.

Fix: initialize ``content_streamed = False`` before the loop, alongside
the other per-request stream state. This AST test pins the invariant:
an assignment to ``content_streamed`` must appear before the v3
``for iteration in range(MAX_TOOL_ITERATIONS)`` loop inside
``add_message_stream``.
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


def test_content_streamed_initialized_before_tool_loop():
    with open(_AGENTS_PATH) as f:
        source = f.read()
    tree = ast.parse(source)
    func = _find_v3_function(tree)

    loop_lineno = None
    assign_lineno = None
    for node in ast.walk(func):
        # The v3 tool loop: ``for iteration in range(MAX_TOOL_ITERATIONS):``
        if (
            isinstance(node, ast.For)
            and isinstance(node.target, ast.Name)
            and node.target.id == "iteration"
            and loop_lineno is None
        ):
            loop_lineno = node.lineno
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "content_streamed":
                    if assign_lineno is None or node.lineno < assign_lineno:
                        assign_lineno = node.lineno

    assert loop_lineno is not None, "v3 tool loop not found"
    assert assign_lineno is not None, "content_streamed is never assigned"
    assert assign_lineno < loop_lineno, (
        f"content_streamed is first assigned at line {assign_lineno} but "
        f"the v3 tool loop starts at line {loop_lineno}. A first-iteration "
        f"break (budget guard / tool-call loop guard) leaves the variable "
        f"unbound, and the post-loop read crashes the SSE stream with "
        f"UnboundLocalError. Initialize it before the loop."
    )
