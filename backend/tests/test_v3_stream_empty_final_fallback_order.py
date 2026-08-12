"""Regression: apply empty-content fallback before final delta emit in v3 stream.

When the tool loop ends without a usable assistant text (for example after
budget/guardrail stop paths), the stream must set a fallback response BEFORE
emitting the final buffered delta. If the delta emit runs first, clients that
render only deltas can appear to stop at "Generating response" with no answer.

This test pins the ordering inside ``add_message_stream``:
- fallback block: ``if not (assistant_content or "").strip():``
- buffered final delta block: ``if not content_streamed: yield delta``

Required order: fallback block appears before delta block.
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


def test_empty_content_fallback_runs_before_final_delta_emit():
    with open(_AGENTS_PATH) as f:
        source = f.read()
    tree = ast.parse(source)
    func = _find_v3_function(tree)

    fallback_lineno = None
    delta_gate_lineno = None

    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue

        # fallback gate: if not (assistant_content or "").strip():
        if (
            isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Call)
            and isinstance(node.test.operand.func, ast.Attribute)
            and node.test.operand.func.attr == "strip"
        ):
            operand = node.test.operand.func.value
            if (
                isinstance(operand, ast.BoolOp)
                and isinstance(operand.op, ast.Or)
                and len(operand.values) == 2
                and isinstance(operand.values[0], ast.Name)
                and operand.values[0].id == "assistant_content"
                and isinstance(operand.values[1], ast.Constant)
                and operand.values[1].value == ""
            ):
                if fallback_lineno is None or node.lineno < fallback_lineno:
                    fallback_lineno = node.lineno

        # final buffered delta gate: if not content_streamed:
        if (
            isinstance(node.test, ast.UnaryOp)
            and isinstance(node.test.op, ast.Not)
            and isinstance(node.test.operand, ast.Name)
            and node.test.operand.id == "content_streamed"
        ):
            # keep the LAST such gate in the function; that's the post-loop emit
            if delta_gate_lineno is None or node.lineno > delta_gate_lineno:
                delta_gate_lineno = node.lineno

    assert fallback_lineno is not None, "empty-content fallback gate not found"
    assert delta_gate_lineno is not None, "final buffered delta gate not found"
    assert fallback_lineno < delta_gate_lineno, (
        f"Empty-content fallback starts at line {fallback_lineno} but final "
        f"delta gate starts at line {delta_gate_lineno}. Apply fallback first "
        f"so the stream always emits user-visible content on stop paths."
    )
