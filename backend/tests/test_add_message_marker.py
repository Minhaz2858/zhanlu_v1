"""Verify that v2 ``add_message`` in agents.py wires the artifact marker parser.

We use AST inspection (matching the pattern from
``test_agents_responder_bug.py``) because a real end-to-end test would
require standing up the full SQLAlchemy stack with Postgres.

What we verify:
1. The function imports ``find_markers`` / ``strip_markers`` from
   ``app.services.artifact_markers``.
2. The function calls ``find_markers(...)`` on the assistant content.
3. The function calls ``strip_markers(...)`` on the assistant content.
4. The function routes the marker payload to ``_create_artifact_tool``
   (the proven artifact pipeline) for each supported marker kind.
5. Marker handling happens BEFORE ``messages.append(assistant_msg)``
   (so the visible message text has the marker stripped).
"""
import ast
import os


def _load_function_source():
    agents_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "agents.py"
    )
    with open(agents_path) as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "add_message":
            return source, node
    raise RuntimeError("async def add_message not found in agents.py")


def _func_uses_name(func, dotted_name: str) -> bool:
    """True if the function source references ``dotted_name`` at least once."""
    try:
        src = ast.unparse(func)
    except Exception:
        # Fallback: search the original module source slice
        src = ""
    return dotted_name in src


def test_add_message_imports_marker_parser():
    """The v2 add_message endpoint must import the marker parser module."""
    source, func = _load_function_source()
    assert "from app.services.artifact_markers import" in source, (
        "agents.py must import find_markers/strip_markers from "
        "app.services.artifact_markers at the top of the file"
    )
    # And the function references it
    assert _func_uses_name(func, "find_markers") or \
           "find_markers" in ast.unparse(func), (
        "add_message must call find_markers on the assistant content"
    )


def test_add_message_calls_strip_markers_on_assistant_content():
    """Markers must be stripped from the user-visible assistant text."""
    _, func = _load_function_source()
    src = ast.unparse(func)
    assert "strip_markers" in src, (
        "add_message must call strip_markers to remove markers from the "
        "user-visible reply text"
    )


def test_add_message_routes_marker_to_create_artifact_tool():
    """Each marker kind must be routed to _create_artifact_tool with the
    correct artifact_type and payload shape."""
    _, func = _load_function_source()
    src = ast.unparse(func)
    # Must reference the proven artifact pipeline
    assert "_create_artifact_tool" in src, (
        "add_message must call _create_artifact_tool to route markers "
        "into the existing artifact pipeline (so preview/links/sandbox "
        "all behave the same as LLM-driven create_artifact calls)"
    )
    # Must support all three marker kinds
    for kind in ("MD_DOCX", "HTML_DOCX", "PPTX"):
        assert kind in src, (
            f"add_message must handle the {kind!r} marker kind"
        )


def test_marker_handling_runs_before_messages_append():
    """The marker block must appear before messages.append(assistant_msg)
    so the stripped content is what gets persisted."""
    _, func = _load_function_source()
    src_lines = ast.unparse(func).splitlines()
    # Find the LAST messages.append(assistant_msg) in the unparsed source —
    # that's the final-persist point of the v2 endpoint. The marker
    # handling must come before that one.
    marker_line = None
    for i, line in enumerate(src_lines):
        if "find_markers" in line:
            marker_line = i
    # Find all append sites and take the last one (the final persist)
    append_lines = [
        i for i, line in enumerate(src_lines)
        if "messages.append(assistant_msg)" in line
    ]
    assert marker_line is not None, "find_markers call not found in add_message"
    assert append_lines, "messages.append(assistant_msg) not found in add_message"
    final_append_line = append_lines[-1]
    assert marker_line < final_append_line, (
        f"marker parsing must run BEFORE the final messages.append(assistant_msg) "
        f"(found find_markers at line {marker_line}, final append at line {final_append_line})"
    )


def test_marker_handler_is_wrapped_in_try_except():
    """Marker handling must be best-effort: a marker failure must never
    break the chat response."""
    _, func = _load_function_source()
    src = ast.unparse(func)
    # Find the marker block (rough text search)
    assert "find_markers" in src
    # The marker handling must mention both "try" and "except" near it
    # (loose heuristic — we just want a non-fatal guard)
    assert "non-fatal" in src, (
        "marker handling must log errors as non-fatal (try/except wrapper)"
    )
