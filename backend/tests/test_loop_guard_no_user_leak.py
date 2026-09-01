"""Regression test for the loop-guard user-facing text leak.

Root cause: when ``_detect_tool_call_loop`` returns a hit, the
loop-guard block in ``add_message`` (v2) and ``add_message_stream`` (v3)
sets ``assistant_content = nudge`` and emits the nudge as a ``delta``
SSE event / final assistant message. The nudge text
(``"Tool 'skills' was already called 3 times. Use the result you have
and produce your final answer. Do not call it again."``) is internal
LLM-facing scaffolding — it is fed to the LLM via
``llm_messages.append({"role": "user", "content": nudge})`` so the
model can be told to wrap up.

But the SAME text is also sent to the user as the assistant's reply
in the chat UI. Users see "Tool 'skills' was already called 3
times..." inside the assistant bubble, which leaks the internal
loop-guard mechanism and reads as a broken / unprofessional response.

This test pins down the fix: when the loop-guard trips, the
user-facing assistant content must be a clean, human-readable message
that does NOT mention internal tool names, call counts, or LLM
instructions. The internal LLM-facing nudge can still be appended to
``llm_messages`` (it's correct as a model-facing instruction), but it
must not be the same string that the user sees.

A pure end-to-end test would require a real LLM + DB; this test does
the minimum reproducible check by parsing the source AST and asserting
that the loop-guard branch sets a user-friendly ``assistant_content``
distinct from the LLM-facing nudge.
"""
import ast
import os
import re

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


def _find_loop_guard_blocks(source):
    """Return the source line ranges of every
    ``if loop_info is not None:`` block that wraps an
    ``assistant_content = nudge`` (or equivalent) assignment.

    The codebase has three of these (v2 main, v2 resume, v3 stream).
    All three must be fixed.
    """
    lines = source.splitlines()
    out = []
    text = source
    for m in re.finditer(r"if loop_info is not None:", text):
        # Find the end of the if-block by counting indent
        start = text.count("\n", 0, m.start())  # 0-indexed line
        out.append(start + 1)  # 1-indexed for humans
    return out


def test_loop_guard_user_facing_text_does_not_mention_internal_tool_call_count():
    """The user-facing text in the loop-guard block must not be the
    internal scaffolding nudge (``nudge`` variable, which contains
    "Tool 'X' was already called N times. Use the result you have and
    produce your final answer. Do not call it again.").
    """
    src = _load_source()
    for line in _find_loop_guard_blocks(src):
        # Read this line + the next 30 lines to span the whole if-block
        block = "\n".join(src.splitlines()[line - 1: line + 30])
        # Buggy pattern: assistant_content is assigned to the same
        # `nudge` variable that is also fed to llm_messages. Both are
        # internal-LLM-facing scaffolding text, not a user-friendly
        # message.
        bad = re.search(
            r"assistant_content\s*=\s*nudge\b",
            block,
        )
        assert not bad, (
            f"Loop-guard block at line ~{line} sets `assistant_content = "
            f"nudge` — the LLM-facing scaffolding text becomes the "
            f"user-visible assistant message. Use a separate "
            f"user-friendly string for the chat UI."
        )


def test_loop_guard_user_facing_text_present():
    """Pin down that there's a user-facing message in the loop-guard
    block at all (not just a silent break). Otherwise the user would
    see an empty assistant bubble."""
    src = _load_source()
    for line in _find_loop_guard_blocks(src):
        block = "\n".join(src.splitlines()[line - 1: line + 40])
        # Look for an assistant_content = "..." (single-line) or
        # assistant_content = ( "..." "..." ) (multi-line parenthesized),
        # or a `user_facing` variable (used by the v3 stream delta emit).
        user_facing = re.search(
            r"(?:assistant_content|user_facing)\s*=\s*"
            r"(?:\"[^\"]+\"|\'[^\']+\'|\(\s*(?:\"[^\"]+\"|\'[^\']+\')[\s\S]*?\))",
            block,
        )
        assert user_facing, (
            f"Loop-guard block at line ~{line} has no plain user-facing "
            f"assistant_content / user_facing assignment. The user would "
            f"see an empty bubble when the guard trips. Provide a short, "
            f"friendly message instead."
        )
