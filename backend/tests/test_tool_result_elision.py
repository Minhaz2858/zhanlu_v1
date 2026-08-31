"""Tests for tool result elision after synthesis (Layer 3 of context-overflow fix).

Root cause (deep research 2026-08-25):
- After ask_data_agent returns 234 rows, the raw JSON sits in the LLM
  context forever (until microcompact clears it).
- For a 3-call turn, that's 3 × ~10K = 30K tokens of dead raw rows.
- The LLM doesn't need them after synthesis — the data is already in
  the UI DataTableCard.

Fix: Add _elide_consumed_tool_results(messages, synthesized_text) that
replaces consumed ask_data_agent tool results with 1-line summaries:
    [234 rows, contract_performance, totals: ¥362.4M revenue, 51,602 units]
Saves 8-10K tokens per old result, 24-30K total for a 3-call turn.

Run in-container:
  /usr/local/bin/python3.11 -c "import sys; sys.path.insert(0, '/app/venv/lib/python3.11/site-packages'); sys.path.insert(0, '/app'); import pytest; exit(pytest.main(['-v', 'tests/test_tool_result_elision.py']))"
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))


# ── Test 1: function exists and is importable ──────────────────────────────


def test_elide_consumed_tool_results_exists():
    """The elision function must be defined and importable."""
    from app.routers.agents import _elide_consumed_tool_results
    assert callable(_elide_consumed_tool_results)


# ── Test 2: replaces ask_data_agent tool results with summaries ───────────


def test_elision_replaces_ask_data_agent_results():
    """A 10K-token ask_data_agent tool result must be replaced with a
    short summary (200-500 tokens) after elision."""
    from app.routers.agents import _elide_consumed_tool_results

    big_content = "x" * 40_000  # ~10K tokens
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "ask_data_agent", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": big_content},
    ]
    synthesized_text = (
        "## Executive Summary\n"
        "Contract performance for July 2026: 218 contracts, "
        "¥362.4M total revenue, 51,602 units.\n"
    )

    result = _elide_consumed_tool_results(messages, synthesized_text)
    # The tool message should now be much shorter
    new_tool_content = result[1]["content"]
    assert len(new_tool_content) < 1000, (
        f"After elision, the tool content should be < 1000 chars "
        f"(a summary). Got {len(new_tool_content)} chars. The raw "
        f"10K-token content is still in context."
    )
    # The summary should mention it's a summary
    assert "[" in new_tool_content or "consumed" in new_tool_content.lower(), (
        f"Summary should be marked. Got: {new_tool_content[:200]}"
    )


# ── Test 3: preserves non-ask_data_agent tool results ─────────────────────


def test_elision_preserves_other_tool_results():
    """Only ask_data_agent results get elided. Other tool results
    (e.g. ask_text_agent) must be preserved."""
    from app.routers.agents import _elide_consumed_tool_results

    big_text_agent_content = "y" * 40_000  # 10K tokens
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "ask_text_agent", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": big_text_agent_content},
    ]
    synthesized_text = "Some analysis."

    result = _elide_consumed_tool_results(messages, synthesized_text)
    # The tool content should be UNCHANGED (not ask_data_agent)
    assert result[1]["content"] == big_text_agent_content, (
        f"ask_text_agent result was elided but it should be preserved. "
        f"Got {len(result[1]['content'])} chars (was {len(big_text_agent_content)})."
    )


# ── Test 4: doesn't double-elide already-summarized results ───────────────


def test_elision_idempotent():
    """Calling elision twice must be idempotent (no further changes on
    the second call). This prevents runaway modifications."""
    from app.routers.agents import _elide_consumed_tool_results

    big_content = "x" * 40_000
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "ask_data_agent", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": big_content},
    ]
    synthesized_text = "Analysis here."

    first_pass = _elide_consumed_tool_results(messages, synthesized_text)
    second_pass = _elide_consumed_tool_results(first_pass, synthesized_text)
    # The second pass should produce identical output to the first
    assert first_pass[1]["content"] == second_pass[1]["content"], (
        "Elision is not idempotent. The second call changed the content."
    )


# ── Test 5: handles missing synthesis gracefully ──────────────────────────


def test_elision_handles_missing_synthesis():
    """If synthesized_text is empty or missing, the function should
    still elide (use a generic summary)."""
    from app.routers.agents import _elide_consumed_tool_results

    big_content = "x" * 40_000
    messages = [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_1", "type": "function",
             "function": {"name": "ask_data_agent", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "call_1", "content": big_content},
    ]

    # Empty synthesis — should still elide with generic summary
    result = _elide_consumed_tool_results(messages, "")
    assert len(result[1]["content"]) < 1000, (
        f"Even with empty synthesis, the tool result should be elided. "
        f"Got {len(result[1]['content'])} chars."
    )


# ── Test 6: wires into the v3 stream loop ────────────────────────────────


def test_elision_wired_into_v3_stream_loop():
    """The _elide_consumed_tool_results function must be called from
    inside the v3 stream loop AFTER the synthesis step completes.
    Otherwise the elision has no effect on the actual flow."""
    import inspect
    from app.routers import agents
    src = inspect.getsource(agents)
    # The function must be called at least once in agents.py
    call_count = src.count("_elide_consumed_tool_results(")
    assert call_count >= 2, (
        f"_elide_consumed_tool_results is defined but called only "
        f"{call_count} times in agents.py. Need at least 2 calls: "
        f"one in the empty-bubble synthesis path and one in the "
        f"apology-guard synthesis path."
    )
