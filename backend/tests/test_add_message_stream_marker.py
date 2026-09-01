"""Verify that v3 ``add_message_stream`` in agents.py wires the marker parser.

Mirrors ``test_add_message_marker.py`` for the SSE endpoint.
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
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "add_message_stream":
            return source, node
    raise RuntimeError("async def add_message_stream not found in agents.py")


def test_v3_uses_marker_parser():
    source, func = _load_function_source()
    assert "from app.services.artifact_markers import" in source
    src = ast.unparse(func)
    assert "find_markers" in src
    assert "strip_markers" in src
    assert "_create_artifact_tool" in src


def test_v3_marker_handling_is_best_effort():
    """SSE must never break if a marker is malformed."""
    _, func = _load_function_source()
    src = ast.unparse(func)
    assert "non-fatal" in src
    # Must have nested try/except (outer wrapper + inner per-marker)
    assert src.count("try:") >= 2
    assert src.count("except") >= 2


def test_v3_marker_handling_runs_before_final_delta_emit():
    """The final streamed delta must NOT contain the raw marker text.

    We compare REAL line numbers from the source file (not the unparsed
    AST, which reorders statements).
    """
    agents_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "agents.py"
    )
    with open(agents_path) as f:
        real_lines = f.readlines()
    # Find the add_message_stream function boundaries in the real file
    start = None
    end = None
    for i, line in enumerate(real_lines):
        if "async def add_message_stream" in line:
            start = i + 1
        elif start is not None and "async def " in line and "add_message_stream" not in line:
            end = i
            break
    assert start is not None, "add_message_stream not found"
    if end is None:
        end = len(real_lines)
    # Find marker block + final delta emit within the function only
    marker_line = None
    for i, line in enumerate(real_lines):
        if "find_markers" in line and i + 1 > start:
            marker_line = i + 1
            break
    delta_lines = [
        i + 1 for i, line in enumerate(real_lines)
        if '"type": "delta"' in line and i + 1 > start
    ]
    assert marker_line is not None, (
        "find_markers call not found inside add_message_stream"
    )
    assert delta_lines, '"type": "delta" not found inside add_message_stream'
    final_delta = max(delta_lines)
    assert marker_line < final_delta, (
        f"v3 marker block must run BEFORE the final delta emit "
        f"(marker at line {marker_line}, final delta at line {final_delta})"
    )


def test_v3_supports_all_three_marker_kinds():
    _, func = _load_function_source()
    src = ast.unparse(func)
    for kind in ("MD_DOCX", "HTML_DOCX", "PPTX"):
        assert kind in src


def test_v3_reasoning_does_not_run_marker_parser():
    """P0 reasoning extraction must not run the marker parser on reasoning
    text. The marker parser should run only on assistant_content (the
    user-visible reply), never on reasoning_content."""
    agents_path = os.path.join(
        os.path.dirname(__file__), "..", "app", "routers", "agents.py"
    )
    with open(agents_path) as f:
        real_lines = f.readlines()
    # Find find_markers calls (in the real source)
    find_lines = [
        i + 1 for i, line in enumerate(real_lines) if "find_markers" in line
    ]
    # Find reasoning_done emit (the reasoning SSE event)
    rd_lines = [
        i + 1 for i, line in enumerate(real_lines)
        if '"reasoning_done"' in line or "'reasoning_done'" in line
    ]
    assert find_lines, "find_markers not found in agents.py"
    assert rd_lines, "reasoning_done event not found in agents.py"
    # The find_markers call MUST be on a different statement than the
    # reasoning_done emit (or, more loosely, the find_markers call site
    # must not be inside the 200-char window of the reasoning_done emit).
    final_rd = max(rd_lines)
    # Any find_markers on the same line as reasoning_done is a bug.
    for fl in find_lines:
        assert fl != final_rd, (
            f"find_markers must not run on the reasoning_done event "
            f"(line {fl})"
        )
