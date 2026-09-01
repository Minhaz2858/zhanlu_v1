"""AST + behavioural checks for extract_stream_parts in llm_service.py.

Mirrors the pattern in test_artifact_markers.py / test_agents_responder_bug.py:
imports the helper directly and exercises it on dict-shaped stream chunks
(DeepSeek-R1, Claude, OpenAI o1, no-op) plus malformed inputs.

Also contains AST checks for the SSE event wiring inside agents.py:
add_message_stream must yield reasoning_delta, trace_step, reasoning_done.
"""
import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

LLM = (ROOT / "app" / "services" / "llm_service.py").read_text()
tree = ast.parse(LLM)


def _has_callable(name: str) -> bool:
    return any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        for node in ast.walk(tree)
    )


# ---------------------------------------------------------------------------
# extract_stream_parts — behavioural tests
# ---------------------------------------------------------------------------


def test_extract_stream_parts_function_exists():
    assert _has_callable("extract_stream_parts"), (
        "extract_stream_parts(chunk) must be defined in llm_service.py"
    )


def test_extract_stream_parts_handles_deepseek_reasoning_content():
    """DeepSeek-R1 style: chunk.choices[0].delta.reasoning_content."""
    from app.services.llm_service import extract_stream_parts

    chunk = {"choices": [{"delta": {"reasoning_content": "thinking..."}}]}
    content, reasoning = extract_stream_parts(chunk)
    assert content == ""
    assert reasoning == "thinking..."


def test_extract_stream_parts_handles_claude_thinking():
    """Claude-style: chunk.choices[0].delta.thinking."""
    from app.services.llm_service import extract_stream_parts

    chunk = {"choices": [{"delta": {"thinking": "considering..."}}]}
    content, reasoning = extract_stream_parts(chunk)
    assert content == ""
    assert reasoning == "considering..."


def test_extract_stream_parts_handles_openai_o1_reasoning():
    """OpenAI o1 style: chunk.choices[0].delta.reasoning."""
    from app.services.llm_service import extract_stream_parts

    chunk = {"choices": [{"delta": {"reasoning": "analyzing..."}}]}
    content, reasoning = extract_stream_parts(chunk)
    assert content == ""
    assert reasoning == "analyzing..."


def test_extract_stream_parts_returns_content_and_reasoning():
    """When both fields are present, both are returned."""
    from app.services.llm_service import extract_stream_parts

    chunk = {"choices": [{"delta": {"content": "Hello", "reasoning_content": "think"}}]}
    content, reasoning = extract_stream_parts(chunk)
    assert content == "Hello"
    assert reasoning == "think"


def test_extract_stream_parts_noop_when_missing():
    """Provider returns no reasoning: returns empty strings, no exception."""
    from app.services.llm_service import extract_stream_parts

    chunk = {"choices": [{"delta": {"content": "hi"}}]}
    content, reasoning = extract_stream_parts(chunk)
    assert content == "hi"
    assert reasoning == ""


def test_extract_stream_parts_safe_on_malformed_chunk():
    """Empty chunk, missing choices, missing delta: all safe."""
    from app.services.llm_service import extract_stream_parts

    for bad in [{}, {"choices": []}, {"choices": [{}]}, {"choices": [{"delta": {}}]}]:
        content, reasoning = extract_stream_parts(bad)
        assert content == ""
        assert reasoning == ""


# ---------------------------------------------------------------------------
# agents.py — AST structural checks for SSE event wiring
# ---------------------------------------------------------------------------

AGENTS = (ROOT / "app" / "routers" / "agents.py").read_text()
agents_tree = ast.parse(AGENTS)


def _find_function(name: str):
    for node in ast.walk(agents_tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _function_contains(node, needle: str) -> bool:
    return needle in ast.unparse(node)


def test_add_message_stream_exists():
    fn = _find_function("add_message_stream")
    assert fn is not None, "add_message_stream must exist in agents.py"


def test_add_message_stream_yields_reasoning_event():
    """The v3 endpoint must surface some reasoning-shaped SSE event.

    Today: a single 'reasoning_done' event at the end (non-streaming
    _call_llm_with_tools path). Future: per-chunk 'reasoning_delta' if the
    streaming LLM migration lands. Either name satisfies the protocol —
    the frontend consumer code handles both. ast.unparse uses single
    quotes, so the search is quote-agnostic.
    """
    fn = _find_function("add_message_stream")
    assert fn is not None
    body = ast.unparse(fn)
    assert (
        "'reasoning_done'" in body or "'reasoning_delta'" in body
    ), "add_message_stream must emit a reasoning-shaped SSE event"


def test_add_message_stream_yields_trace_step():
    fn = _find_function("add_message_stream")
    assert fn is not None
    assert _function_contains(
        fn, "'trace_step'"
    ), "Must yield a 'trace_step' SSE event per tool call"


def test_add_message_stream_yields_reasoning_done():
    fn = _find_function("add_message_stream")
    assert fn is not None
    assert _function_contains(
        fn, "'reasoning_done'"
    ), "Must yield a 'reasoning_done' SSE event after the loop"


def test_add_message_stream_persists_reasoning_on_assistant_msg():
    """The assistant message must carry a 'reasoning' key before the final done event."""
    fn = _find_function("add_message_stream")
    assert fn is not None
    body = ast.unparse(fn)
    assert '"reasoning"' in body or "'reasoning'" in body, (
        "Assistant message must be assigned a 'reasoning' field"
    )
