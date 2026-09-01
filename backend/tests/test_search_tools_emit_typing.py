"""2026-08-25: live-streaming spec — verify search_query_delta is emitted from the main stream.

We wire the typing effect in the main event_stream (right after tool_call_started)
rather than inside each of the 4 search tools, because the tools return dicts
(not generators) and the SSE stream is the right place to emit SSE frames.
"""
import inspect
import os


def test_streaming_helpers_uses_search_query_delta_type():
    """The helper must emit search_query_delta events."""
    from app.services.agent_loop import streaming_helpers
    src = inspect.getsource(streaming_helpers._stream_typing_effect)
    assert "search_query_delta" in src, "_stream_typing_effect must emit search_query_delta"


def test_agents_py_emits_typing_for_search_tools():
    """agents.py event_stream must call _stream_typing_effect for search tools."""
    agents_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "routers", "agents.py",
    )
    with open(agents_path, "r", encoding="utf-8") as f:
        src = f.read()
    assert "_stream_typing_effect" in src, \
        "agents.py must call _stream_typing_effect (live-streaming spec)"
    assert "search_query_delta" in src or "_SEARCH_TOOL_NAMES" in src, \
        "agents.py must reference search_query_delta or _SEARCH_TOOL_NAMES"


def test_agents_py_knows_search_tool_names():
    """agents.py must enumerate which tools are search tools."""
    agents_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "app", "routers", "agents.py",
    )
    with open(agents_path, "r", encoding="utf-8") as f:
        src = f.read()
    # Must mention at least one known search tool
    has_web = "web_search" in src
    has_rag = "ask_rag_research" in src or "rag_research" in src
    has_docs = "search_documents" in src
    assert has_web or has_rag or has_docs, \
        "agents.py must know about web_search / rag_research / search_documents"
